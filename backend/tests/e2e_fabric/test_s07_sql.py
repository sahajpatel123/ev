
def test_s07_sql_select(scratch_db):
    from app.fabric.sql_lane import query_scratch
    rows = query_scratch(str(scratch_db), "SELECT body FROM notes")
    assert rows and rows[0][0] == "hello"
