"""Serve name/schema discovery from cached public metadata, never object tables."""
import re
import sqlite3
import time
from .sql import TOKEN, validate_statement


def _identifier(token):
    if token.startswith(("'", '--', '/*')):
        return None
    return token[1:-1].replace(token[0]*2, token[0]) if token.startswith(('"', '`', '[')) else token


def cached_query(sql, schemas, selected_views, available_views, max_rows=50):
    tokens = [m for m in TOKEN.finditer(sql) if not m.group().startswith(('--', '/*'))]
    ids = [(_identifier(m.group()) or '').casefold() for m in tokens]
    if not any(x in ('sqlite_master', 'sqlite_schema', 'pragma_table_info') for x in ids):
        return None
    started = time.perf_counter()
    result = dict(columns=[], rows=[], error=None, truncated=False, row_count=None,
                  rows_saved=0, actual_reads=[], result_kind='schema_metadata',
                  metadata_source='cached_public_view_inventory')
    con = sqlite3.connect(':memory:')
    try:
        validate_statement(sql)
        con.execute('CREATE TABLE _catalog_views(type TEXT,name TEXT,tbl_name TEXT)')
        con.executemany('INSERT INTO _catalog_views VALUES(?,?,?)',
                        [('view', v, v) for v in available_views])
        tables = {'_catalog_views'}
        edits = []
        for i, token in enumerate(ids):
            if token in ('sqlite_master', 'sqlite_schema'):
                edits.append((tokens[i].start(), tokens[i].end(), '"_catalog_views"'))
            elif token == 'pragma_table_info':
                if i+3 >= len(tokens) or tokens[i+1].group() != '(' or tokens[i+3].group() != ')':
                    raise ValueError('Cached table_info requires one literal selected View name.')
                value = tokens[i+2].group()
                if not re.fullmatch(r"'(?:''|[^'])*'", value):
                    raise ValueError('Cached table_info requires one literal selected View name.')
                view = value[1:-1].replace("''", "'")
                matches = [v for v in selected_views if v.casefold() == view.casefold()]
                if not matches:
                    raise ValueError('Column metadata is available only for selected Views: '+view)
                view = matches[0]
                table = f'_catalog_columns_{i}'
                con.execute(f'CREATE TABLE {table}(cid INTEGER,name TEXT,type TEXT,"notnull" INTEGER,dflt_value TEXT,pk INTEGER)')
                con.executemany(f'INSERT INTO {table} VALUES(?,?,?,?,?,?)',
                                [(n, c, '', None, None, None) for n,c in enumerate(schemas[view])])
                tables.add(table)
                edits.append((tokens[i].start(), tokens[i+3].end(), '"'+table+'"'))
        cached_sql = sql
        for start,end,value in sorted(edits, reverse=True):
            cached_sql = cached_sql[:start]+value+cached_sql[end:]
        result['cached_sql'] = cached_sql
        con.execute('PRAGMA query_only=ON')
        con.setconfig(sqlite3.SQLITE_DBCONFIG_DQS_DML, False)
        def authorize(action, first, second, database, context):
            if action == sqlite3.SQLITE_READ:
                return sqlite3.SQLITE_OK if database in (None,'main') and first in tables else sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_FUNCTION:
                return sqlite3.SQLITE_DENY if (second or '').lower() in ('load_extension','readfile','writefile') else sqlite3.SQLITE_OK
            return sqlite3.SQLITE_OK if action in (sqlite3.SQLITE_SELECT,sqlite3.SQLITE_RECURSIVE) else sqlite3.SQLITE_DENY
        con.set_authorizer(authorize)
        deadline = time.monotonic()+1
        con.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        cursor = con.execute(cached_sql)
        result['columns'] = [x[0] for x in cursor.description]
        rows = cursor.fetchmany(max_rows+1)
        result.update(rows=[list(x) for x in rows[:max_rows]], truncated=len(rows)>max_rows,
                      rows_saved=min(len(rows),max_rows), row_count=None if len(rows)>max_rows else len(rows))
    except (ValueError, sqlite3.Error) as exc:
        result['error'] = str(exc)
    finally:
        con.close()
        result['elapsed_ms'] = (time.perf_counter()-started)*1000
    return result
