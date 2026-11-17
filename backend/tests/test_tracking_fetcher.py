"""arXiv feed 解析测试（离线，不访问网络）。"""
from app.tracking.arxiv_fetcher import parse_arxiv_feed

FEED = """<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2401.12345v2</id>
    <title>A Novel Retrieval Method</title>
    <summary>We release code at https://github.com/team/repo for this method.</summary>
    <published>2024-01-15T00:00:00Z</published>
    <author><name>Alice Zhang</name></author>
    <author><name>Bob Li</name></author>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2312.99999v1</id>
    <title>Another Study</title>
    <summary>No code released in this abstract.</summary>
    <published>2023-12-01T00:00:00Z</published>
    <author><name>Carol</name></author>
  </entry>
</feed>"""


def test_parse_feed_fields():
    items = parse_arxiv_feed(FEED)
    assert len(items) == 2
    first = items[0]
    # 版本号被去除，保留 base arxiv id
    assert first["arxiv_id"] == "2401.12345"
    assert first["title"] == "A Novel Retrieval Method"
    assert first["authors"] == "Alice Zhang, Bob Li"
    assert first["published_at"] == "2024-01-15"
    # 从摘要检测 GitHub
    assert first["github_url"] == "https://github.com/team/repo"
    # 无链接时不编造
    assert items[1]["github_url"] is None
