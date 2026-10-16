"""Local, versioned body cache and per-stage checkpoints. No business table writes."""
import hashlib
import json
import logging
import os
import tempfile
import time
from datetime import date
from pathlib import Path

from .config import CACHE_DIR, TEXT_MODEL_CHAIN, TEXT_REASONING_EFFORT, ENABLE_WEB_SEARCH
from .result_schema import validation_errors

logger = logging.getLogger(__name__)


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def pipeline_version():
    root = Path(__file__).resolve().parent
    sources = ["analysis_stage.py", "llm_agent.py", "result_schema.py", "config.py",
               "contact_verifier.py", "direction_verifier.py", "api_client.py", "utils.py",
               "model_telemetry.py", "final_review.py"]
    hasher = hashlib.sha256()
    for name in sources:
        hasher.update((root / name).read_bytes())
    hasher.update(json.dumps([TEXT_MODEL_CHAIN, TEXT_REASONING_EFFORT, ENABLE_WEB_SEARCH]).encode())
    return hasher.hexdigest()


class RunStore:
    def __init__(self, root=None, version=None):
        self.root = Path(root) if root is not None else CACHE_DIR / "runs"
        self.version = version or pipeline_version()

    def _read(self, path):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, path, data):
        temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(data, handle, ensure_ascii=False, indent=2)
            os.replace(temporary, path)
            return True
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("本地运行缓存写入失败（继续业务处理）: %s", exc)
            return False
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def body_path(self, source, url):
        return self.root / "content" / (digest(source + "\n" + url) + ".json")

    def get_content(self, source, url, max_age=7 * 86400):
        data = self._read(self.body_path(source, url))
        content, created = data.get("content"), data.get("created_at")
        if (data.get("source") != source or data.get("url") != url
                or not isinstance(created, (int, float)) or not 0 <= time.time() - created <= max_age
                or not isinstance(content, str) or not content.strip()):
            return None
        return content

    def save_content(self, source, url, content):
        return self._write(self.body_path(source, url),
                           {"source": source, "url": url, "content": content, "created_at": time.time()})

    def checkpoint_path(self, source, row_index, url):
        return self.root / "stages" / (digest(f"{source}\n{row_index}\n{url}") + ".json")

    def load_stages(self, source, row_index, url, content):
        data = self._read(self.checkpoint_path(source, row_index, url))
        if (data.get("version") != self.version or data.get("content_hash") != digest(content)
                or data.get("source") != source or data.get("url") != url or data.get("row_index") != row_index):
            return {}
        stages = data.get("stages", {})
        if not isinstance(stages, dict):
            return {}
        return {stage: entry["result"] for stage, entry in stages.items()
                if isinstance(entry, dict) and entry.get("success") is True
                and (stage != "stage1" or entry.get("analysis_date") == date.today().isoformat())
                and not validation_errors(entry.get("result"), stage,
                    allow_empty_label1=stage == 'stage3')}

    def load_stage_evidence(self, source, row_index, url, content):
        valid = self.load_stages(source, row_index, url, content)
        data = self._read(self.checkpoint_path(source, row_index, url))
        return {stage: data['stages'][stage].get('evidence', []) for stage in valid
                if isinstance(data['stages'][stage].get('evidence', []), list)}

    def save_review(self, source, row_index, url, report):
        path = self.root / 'reviews' / (digest(f'{source}\n{row_index}\n{url}') + '.json')
        return self._write(path, {'source': source, 'row_index': row_index, 'url': url,
                                  'created_at': time.time(), 'review': report})

    def load_stage_warnings(self, source, row_index, url, content):
        valid = self.load_stages(source, row_index, url, content)
        data = self._read(self.checkpoint_path(source, row_index, url))
        return {stage: data['stages'][stage].get('warnings', []) for stage in valid
                if isinstance(data['stages'][stage].get('warnings', []), list)}

    def save_stage(self, source, row_index, url, content, stage, success, result, error, evidence=None, warnings=None):
        path = self.checkpoint_path(source, row_index, url)
        data = self._read(path)
        if data.get("version") != self.version or data.get("content_hash") != digest(content):
            data = {"source": source, "row_index": row_index, "url": url, "version": self.version,
                    "content_hash": digest(content), "stages": {}}
        if not isinstance(data.get("stages"), dict):
            data["stages"] = {}
        data["stages"][stage] = {"success": bool(success), "result": result if success else {},
                                "evidence": evidence if isinstance(evidence, list) else [],
                                "warnings": warnings if isinstance(warnings, list) else [],
                                "error": error, "created_at": time.time(),
                                "analysis_date": date.today().isoformat()}
        return self._write(path, data)
