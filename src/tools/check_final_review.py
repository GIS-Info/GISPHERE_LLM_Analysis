"""Two paid synthetic Opus checks; never load Sheets/Excel/browser workflows."""
import json
import argparse
from datetime import datetime, timezone

from ..core.config import CACHE_DIR, PROJECT_ROOT
from ..core.final_review import candidate_fields, review_final_result
from ..core.observability import flush_traces


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', choices=('all', 'supported_external_contact', 'wrong_email'), default='all')
    selected = parser.parse_args().case
    cases = json.loads((PROJECT_ROOT / 'tests/fixtures/evaluation_cases.json').read_text(encoding='utf-8'))['cases']
    case = cases[0]
    fields = candidate_fields({}, {k: v for result in case['expected'].values() for k, v in result.items()})
    source = case['text'].replace('Professor Jane Example, jane@example.invalid.', 'Jane Example. See the staff profile for qualifications and email.')
    # Exactly one adopted page snapshot, not a list of search candidates.
    evidence = [{'kind': 'contact_decision_page', 'url': 'https://example.invalid/staff/jane',
        'text': 'Example University staff profile: Professor Jane Example. Email: jane@example.invalid.',
        'decision_fields': ['Contact_Name', 'Contact_Email']}]
    reports = []
    try:
        for name, values in (
            ('supported_external_contact', fields),
            ('wrong_email', dict(fields, Contact_Email='invented@example.invalid')),
        ):
            if selected not in ('all', name):
                continue
            result = review_final_result(source, values, source_url='https://example.invalid/job', evidence=evidence)
            reports.append({'case': name, 'report': result})
            print(f"{name}: {result['status']} ({result['model']})", flush=True)
            if result['status'] != 'passed':
                print(result.get('summary', ''), flush=True)
                for check in result.get('checks', []):
                    if check['status'] != 'ok':
                        print(f"  {check['field']}: {check['reason']}", flush=True)
        folder = CACHE_DIR / 'review_checks'
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        (folder / (stamp + '.json')).write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding='utf-8')
        by_name = {item['case']: item['report'] for item in reports}
        passed = ('supported_external_contact' not in by_name or
                  by_name['supported_external_contact']['status'] == 'passed')
        wrong = by_name.get('wrong_email')
        rejected = wrong is None or wrong['status'] == 'needs_review' and any(
            check['field'] == 'Contact_Email' and check['status'] != 'ok'
            for check in wrong.get('checks', []))
        print('No business tables, documents, database or email were accessed.')
        return 0 if passed and rejected else 1
    finally:
        flush_traces()


if __name__ == '__main__':
    raise SystemExit(main())
