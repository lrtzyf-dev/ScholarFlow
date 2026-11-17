"""Shared citation-relation principles, not a graph action or source receipt.

This policy guides generation and bounded answer-quality review. It does not
certify any current document, import external records or grant tool access.
"""

RELATION_EVIDENCE_POLICY = (
    "解释引用判定依据时，分开核对引用关系存在及方向、目标作品身份、正文定位和科学主张支持。"
    "A引用B时，A是引用方、B是被引方，A→B的书目证据是A的参考文献中列出B，"
    "正文证据是A中的引用指向B；B的参考文献列出A证明的是B→A，不能反向写成A→B。"
    "可核验参考条目、明确可定位的引用句或可信引用记录可构成不同取证路径，"
    "每条路径都要核对记录来源、方向、目标消歧和版本；DOI可帮助消歧，DOI不是必需项。"
    "不能要求所有格式同时具备正文标记和参考文献表，也不能仅因缺少正文定位就否定已有可核验书目引用。"
    "正文位置缺失或尚未找到时须如实标注；找到引用句不等于其中科学主张已获支持，书目引用边也不等于主张正确。"
    "主题相似本身不能证明引用，共被引需要共同引用方，共同引用需要共同参考文献，不能互换。"
    "这些是取证原则，不证明已核验本轮文件或已实现外部引用索引导入；实际关系写入仍须遵守用户授权与工具边界。"
)
