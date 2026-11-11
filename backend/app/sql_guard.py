"""NLQ SQL 守卫（代码层安全，不依赖提示词）：单条只读查询、表白名单、强制 LIMIT。"""
import sqlglot
from sqlglot import exp

MAX_LIMIT = 1000
FORBIDDEN = (exp.Insert, exp.Delete, exp.Update, exp.Drop, exp.Create, exp.Alter, exp.Merge, exp.Command,
             exp.TruncateTable, exp.Grant) if hasattr(exp, "Grant") else (
    exp.Insert, exp.Delete, exp.Update, exp.Drop, exp.Create, exp.Alter, exp.Merge, exp.Command, exp.TruncateTable)


class Rejected(Exception):
    pass


def check(sql, allowed_tables):
    sql = (sql or "").strip().rstrip(";").strip()
    if not sql:
        raise Rejected("空 SQL")
    try:
        stmts = [s for s in sqlglot.parse(sql, read="trino") if s is not None]
    except Exception as e:  # noqa
        raise Rejected(f"SQL 解析失败: {str(e)[:200]}")
    if len(stmts) != 1:
        raise Rejected("只允许单条语句")
    st = stmts[0]
    if not isinstance(st, (exp.Select, exp.Union, exp.With)) and not isinstance(st, exp.Query):
        raise Rejected(f"只允许 SELECT 查询（收到 {type(st).__name__}）")
    for node in st.walk():
        if isinstance(node, FORBIDDEN):
            raise Rejected(f"包含禁止的操作: {type(node).__name__}")
    ctes = {c.alias_or_name.lower() for c in st.find_all(exp.CTE)}
    allowed = {t.lower() for t in allowed_tables}
    used = set()
    for t in st.find_all(exp.Table):
        name = t.name.lower()
        if not name or name in ctes:
            continue
        if t.args.get("db") and t.args["db"].name.lower() not in ("", ):
            raise Rejected("不允许跨库引用，请直接使用表名")
        if name not in allowed:
            raise Rejected(f"表 {name} 不在允许范围: {sorted(allowed)}")
        used.add(name)
    if not used:
        raise Rejected("查询未引用任何日志表")
    lim = st.args.get("limit")
    if lim is None:
        sql = f"{sql}\nLIMIT 100"
    else:
        try:
            if int(lim.expression.name) > MAX_LIMIT:
                raise Rejected(f"LIMIT 不得超过 {MAX_LIMIT}")
        except (ValueError, AttributeError):
            pass
    return sql, sorted(used)
