"""Generate the fixed synthetic Word QA artifact, never private user content."""
from pathlib import Path
import os
import tempfile
import sys

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="sf_word_qa_")
os.environ["LLM_API_KEY"] = ""
os.environ["LLM_DEMO_MODE"] = "1"
os.environ["EMBEDDING_API_KEY"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.exporter import build_docx

OUTPUT = Path(__file__).resolve().parents[2] / "verification" / "artifact-delivery"
OUTPUT.mkdir(parents=True, exist_ok=True)
sample = r"""## 方法和结果

比较两种合成方法，缺值保持为空。**以下数值仅用于验收。**

| 方法 | 样本数量 | 准确率 |
| --- | --- | --- |
| 方法 A | 24 | 81% |
| 方法 B | 36 | 87% |

差值为 6 个百分点，不据此推断统计显著性。

## 可编辑公式

距离 $d = \sqrt{x^2+y^2}$，分式 $\frac{a_i}{b^2}$。

$$
S = \sum_{i=1}^{n} \frac{i}{n}
$$

矩阵：$\begin{pmatrix}a & b \\ c & d\end{pmatrix}$。

## 可复算过程

- 原始数据与参数保持固定版本。
- 执行保存的脚本后核查均值及缺值数量。

```python
mean = (81 + 87) / 2  # 84
```
"""
sources = [{"title": "合成材料（验收专用）", "revision_id": "synthetic-fixed-revision", "locator_id": "synthetic-locator",
            "quote": "方法 A 为 81%，方法 B 为 87%。", "verification": "source_frozen_semantics_unverified"}]
(OUTPUT / "editable-research.docx").write_bytes(build_docx("研序可编辑成果验收", sample, sources))
print("Saved synthetic editable Word QA artifact")
