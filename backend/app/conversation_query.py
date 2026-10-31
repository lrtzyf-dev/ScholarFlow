"""Bounded conversation search and keyset pagination, including legacy fallback rows.

Search is a literal substring (Chinese and ASCII case-insensitive); SQL returns
only a short excerpt, never full answers, attachments, evidence or checkpoints.
"""
import base64
import json

from sqlalchemy import text

from .conversation_activity import conversation_summaries


class InvalidCursor(ValueError):
    pass


def query_conversations(db, *, scope="active", q="", sort="updated_at", limit=40, cursor=None, ids=None):
    from . import run_store, task_runner

    q = q.strip()
    params = {"q": q, "limit": limit + 1}
    active_runs = list(run_store.active_runs())
    executing = task_runner.running_task_ids()

    def binds(prefix, values):
        keys = []
        for index, value in enumerate(values):
            key = f"{prefix}{index}"
            params[key] = value
            keys.append(f":{key}")
        return ",".join(keys) or "NULL"

    active_sql, executing_sql = binds("run", active_runs), binds("task", executing)
    # Choose a canonical row without loading every ORM object. Existing rows,
    # including archives, always win over a draft or run with the same ID.
    candidates = f"""
    WITH qa AS (
      SELECT conversation_id AS id, substr(question,1,200) AS title,
        min(created_at) OVER (PARTITION BY conversation_id) AS created_at,
        max(created_at) OVER (PARTITION BY conversation_id) AS updated_at,
        row_number() OVER (PARTITION BY conversation_id ORDER BY created_at,id) AS n
      FROM qa_records WHERE conversation_id IS NOT NULL AND conversation_id NOT IN (SELECT id FROM conversations)
    ), candidates AS (
      SELECT id,title,created_at,updated_at,pinned_at,pin_position,archived_at,archive_revision,0 AS priority FROM conversations
      UNION ALL SELECT id,title,created_at,updated_at,NULL,NULL,NULL,0,1 FROM qa WHERE n=1
      UNION ALL SELECT substr(key,14), CASE WHEN length(trim(json_extract(value,'$.text')))>0
        THEN substr(trim(json_extract(value,'$.text')),1,80) ELSE '图片草稿' END,
        updated_at,updated_at,NULL,NULL,NULL,0,2 FROM desktop_state
        WHERE key LIKE 'conversation:%' AND
        (length(trim(json_extract(value,'$.text')))>0 OR json_array_length(json_extract(value,'$.images'))>0)
      UNION ALL SELECT conversation_id,substr(question,1,80),created_at,updated_at,NULL,NULL,NULL,0,3
        FROM foreground_runs WHERE status IN ('running','interrupted','cancelled','failed') OR id IN ({active_sql})
      UNION ALL SELECT conversation_id,title,created_at,updated_at,NULL,NULL,NULL,0,4 FROM tasks
        WHERE type='agent' AND (status IN ('pending','running') OR id IN ({executing_sql}))
    ), ranked AS (
      SELECT *,row_number() OVER (PARTITION BY id ORDER BY priority,created_at,title) AS n FROM candidates
      WHERE id IS NOT NULL AND id<>'' AND id NOT IN (SELECT id FROM deleted_conversations)
    ), canonical AS (SELECT * FROM ranked WHERE n=1)
    """
    conditions = []
    if scope != "all":
        conditions.append("c.archived_at IS " + ("NOT NULL" if scope == "archived" else "NULL"))
    if ids is not None:
        conditions.append(f"c.id IN ({binds('cid', ids)})")
    join = ""
    excerpt = "NULL AS match_field,NULL AS match_text,NULL AS match_record_id,NULL AS match_run_id"
    if q:
        candidates += """,
        sources AS (
          SELECT id,'title' AS field,title AS content,NULL AS record_id,NULL AS run_id,0 AS priority,updated_at AS at FROM canonical
          UNION ALL SELECT id,'id',id,NULL,NULL,1,updated_at FROM canonical
          UNION ALL SELECT conversation_id,'question',question,id,NULL,2,created_at FROM qa_records
          UNION ALL SELECT conversation_id,'answer',answer,id,NULL,3,created_at FROM qa_records
          UNION ALL SELECT substr(key,14),'draft',json_extract(value,'$.text'),NULL,NULL,4,updated_at
            FROM desktop_state WHERE key LIKE 'conversation:%'
          UNION ALL SELECT conversation_id,'interrupted',question,NULL,id,5,updated_at FROM foreground_runs
            WHERE status IN ('running','interrupted','cancelled','failed')
          UNION ALL SELECT conversation_id,'interrupted',partial_answer,NULL,id,6,updated_at FROM foreground_runs
            WHERE status IN ('running','interrupted','cancelled','failed')
        ), matching AS (
          SELECT *,row_number() OVER (PARTITION BY id ORDER BY priority,at DESC,record_id DESC,run_id DESC) AS n
          FROM sources WHERE instr(lower(content),lower(:q))>0
        )
        """
        join = "JOIN matching m ON m.id=c.id AND m.n=1"
        excerpt = """m.field AS match_field,
          substr(m.content,max(1,instr(lower(m.content),lower(:q))-40),max(160,length(:q)+80)) AS match_text,
          m.record_id AS match_record_id,m.run_id AS match_run_id"""
    where = " AND ".join(conditions) or "1=1"
    sidebar = sort.startswith("sidebar_")
    field = "created_at" if sort.endswith("created_at") else "updated_at"
    p = "CASE WHEN c.pinned_at IS NULL THEN 1 ELSE 0 END" if sidebar else (
        "CASE WHEN lower(c.id)=lower(:q) OR lower(c.title)=lower(:q) THEN 0 ELSE 1 END" if q else "1")
    rank = "CASE WHEN c.pinned_at IS NULL THEN 0 ELSE coalesce(c.pin_position,-1) END" if sidebar else "0"
    at = f"coalesce(c.{field},c.updated_at,c.created_at,'1970-01-01 00:00:00')"
    if sidebar:
        at = f"CASE WHEN c.pinned_at IS NOT NULL THEN c.pinned_at ELSE {at} END"
    candidates += f", visible AS (SELECT c.*,{p} AS p,{rank} AS rank,{at} AS at,{excerpt} FROM canonical c {join} WHERE {where}) "
    boundary = ""
    if cursor:
        try:
            value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
            if not isinstance(value, list) or len(value) != 7 or value[:3] != [scope, q, sort]:
                raise ValueError()
            p0, r0, t0, id0 = value[3:]
            if (type(p0) is not int or p0 not in (0, 1) or type(r0) is not int or not -1 <= r0 <= 10**9
                    or not isinstance(t0, str) or not 1 <= len(t0) <= 64
                    or not isinstance(id0, str) or not 1 <= len(id0) <= 64):
                raise ValueError()
            params.update(p0=p0, r0=r0, t0=t0, id0=id0)
            boundary = "WHERE p>:p0 OR (p=:p0 AND (rank>:r0 OR (rank=:r0 AND (at<:t0 OR (at=:t0 AND id<:id0)))))"
        except (ValueError, TypeError, UnicodeError) as exc:
            raise InvalidCursor("会话分页游标无效，请刷新列表") from exc
    total, pinned_total = db.execute(text(candidates + "SELECT count(*),coalesce(sum(pinned_at IS NOT NULL),0) FROM visible"), params).one()
    rows = list(db.execute(text(candidates + f"SELECT * FROM visible {boundary} ORDER BY p,rank,at DESC,id DESC LIMIT :limit"), params).mappings())
    more = len(rows) > limit
    rows = rows[:limit]
    # Activity queries are bounded to this page; large bodies stay in SQLite.
    summaries = {item["id"]: item for item in conversation_summaries(db, scope="all", ids=[r["id"] for r in rows])}
    items = []
    for row in rows:
        item = summaries.get(row["id"])
        if item is None:
            continue
        for key in ("title", "created_at", "updated_at", "pinned_at", "pin_position", "archived_at", "archive_revision",
                    "match_field", "match_text", "match_record_id", "match_run_id"):
            item[key] = row[key]
        item["title"] = item["title"] or "新会话"
        items.append(item)
    next_cursor = None
    if more:
        last = rows[-1]
        next_cursor = base64.urlsafe_b64encode(json.dumps([scope, q, sort, last["p"], last["rank"], last["at"], last["id"]]).encode()).decode()
    return {"conversations": items, "total": total, "pinned_total": pinned_total, "next_cursor": next_cursor}
