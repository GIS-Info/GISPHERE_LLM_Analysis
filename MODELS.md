# 可用模型清单（Available Models）

<!-- generated: 2026-05-30 -->
> 自动生成于 **2026-05-30**，数据来源：[New API 网关定价](https://newapi.gisphere.info/api/pricing)。
> 每月首次运行主流程时会自动刷新；也可手动运行 `python -m src.tools.update_models`。
> 项目总览与 LLM 配置说明见 [README.md](README.md#-llm-配置)。

## 如何切换模型

编辑 [`src/core/config.py`](src/core/config.py) 中的两条模型链（按价格由低到高，运行时逐个回退）：

- `TEXT_MODEL_CHAIN`：三阶段文本分析（**全部模型**均可选）；
- `VISION_MODEL_CHAIN`：图片/文档(VLM)提取，**仅从下方「多模态模型」表选取**。

## 当前默认模型链（config.py）

```python
TEXT_MODEL_CHAIN = ['gpt-5.4-mini', 'gemini-2.5-flash', 'claude-opus-4.5']
VISION_MODEL_CHAIN = ['gpt-5.4-mini', 'gemini-2.5-flash', 'claude-opus-4.5']
```

> 以上为生成时自 `config.py` 读取的快照；修改模型链后重新运行 `python -m src.tools.update_models` 可刷新本节。

## 价格说明

价格单位为 **美元 / 百万 tokens (USD / 1M tokens)**，换算自网关倍率：
`输入 = model_ratio × 2`，`输出 = model_ratio × completion_ratio × 2`。

多模态能力由模型名族推断（定价接口未提供该字段），个别模型请以平台文档为准。
非 `openai` 端点（如 `gemini, openai`）已并入「备注」列。

## 多模态模型（21 个，可用于 VISION_MODEL_CHAIN）

支持**文本 + 图片/视觉输入**，可用于 Document AI 与截图 OCR 的 VLM 路径。

| 模型 | 平台 | 输入 $/1M | 输出 $/1M | 备注 |
|:---|:---|---:|---:|:---|
| `gpt-5-mini` | OpenAI | 0.040 | 0.320 |  |
| `gpt-4.1-nano` | OpenAI | 0.100 | 0.400 |  |
| `gpt-4o-mini` | OpenAI | 0.150 | 0.600 |  |
| `gpt-5.4-nano` | OpenAI | 0.200 | 1.250 |  |
| `gemini-3.1-flash-lite` | Google | 0.250 | 1.500 | 端点: gemini, openai |
| `gemini-2.5-flash` | Google | 0.300 | 2.500 | 端点: gemini, openai |
| `gpt-4.1-mini` | OpenAI | 0.400 | 1.600 |  |
| `gemini-3-flash` | Google | 0.500 | 3.000 | 端点: gemini, openai |
| `gpt-5.4-mini` | OpenAI | 0.750 | 4.500 |  |
| `o4-mini` | OpenAI | 1.100 | 4.400 |  |
| `gpt-5-chat` | OpenAI | 1.250 | 10.000 |  |
| `gpt-5.1-chat` | OpenAI | 1.250 | 10.000 |  |
| `gemini-3.5-flash` | Google | 1.500 | 9.000 | 端点: gemini, openai |
| `gpt-5.2-chat` | OpenAI | 1.750 | 14.000 |  |
| `gpt-5.3-codex` | OpenAI | 1.750 | 14.000 |  |
| `gpt-4.1` | OpenAI | 2.000 | 8.000 |  |
| `gpt-4o` | OpenAI | 2.500 | 10.000 |  |
| `gpt-5.4` | OpenAI | 2.500 | 15.000 |  |
| `mistral-document-ai-2512` | Mistral | 3.000 | 9.000 |  |
| `claude-opus-4.5` | Anthropic | 5.000 | 25.000 | 端点: anthropic, openai |
| `gpt-5.5` | OpenAI | 5.000 | 30.000 |  |

## 其他模型（7 个，仅文本 / embedding / 生图等）

不适合作为 `VISION_MODEL_CHAIN` 候选；embedding 与纯生图模型不可用于对话式多模态输入。

> **说明**：`kimi-k2.6`、`deepseek-v4-flash` 等因模型名未命中多模态推断规则而归入本节；
> 若平台文档确认支持图片输入，可手动加入 `VISION_MODEL_CHAIN`，
> 并在 `src/tools/update_models.py` 的 `_VISION_PATTERNS` 中补充对应规则。

| 模型 | 平台 | 输入 $/1M | 输出 $/1M | 备注 |
|:---|:---|---:|---:|:---|
| `text-embedding-3-small` | OpenAI | 0.020 | 0.020 |  |
| `text-embedding-3-large` | OpenAI | 0.130 | 0.130 |  |
| `model-router` | 其他 | 0.750 | 4.500 |  |
| `kimi-k2.6` | Moonshot | 0.950 | 4.000 |  |
| `deepseek-v4-flash` | DeepSeek | 1.030 | 4.120 |  |
| `gpt-image-2` | OpenAI | 75.000 | 150.000 |  |
| `imagen-4.0-generate-001` | 其他 | 75.000 | 75.000 | 端点: image-generation, gemini, openai |

> 共 **28** 个模型：多模态 **21** 个，其他 **7** 个。
