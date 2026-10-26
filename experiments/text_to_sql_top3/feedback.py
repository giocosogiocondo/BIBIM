"""Small, source-only feedback shared by inspection and main SQL generation."""
import difflib
import json
import math
import re


SQL_RULES = '''Use one SQLite SELECT/WITH statement only. Use double quotes for identifiers and single quotes for string values. Qualify columns with View aliases when joining Views.
Use actual supplied View and column names. Only currently selected Views may provide object data; the View score list is discovery metadata, not additional query permission.
A base View and its CustomPset companion describe the same objects and may join on ifc_id. Do not assume IDs match across different entity classes. No other databases or raw tables are available.
View names are already listed in the routing context, and selected column names are already supplied. Prefer checking actual values over repeating schema discovery. Empty rows and NULL are valid results, not SQL errors.'''


def score_context(scores):
    if scores is None:
        return ''
    if not isinstance(scores, dict) or not scores:
        raise ValueError('View scores must be a nonempty mapping')
    if any(not isinstance(k, str) or not isinstance(v, (int, float)) or isinstance(v, bool)
           or not math.isfinite(v) or not 0 <= v <= 1 for k, v in scores.items()):
        raise ValueError('Invalid View score')
    ordered = sorted(scores, key=lambda k: -scores[k])
    return ('View scores for this database (descending original scores; displayed to two decimals; '
            '0.00 does not mean absent; scores are routing preferences, not answer correctness):\n'
            + ', '.join(f'{name}:{scores[name]:.2f}' for name in ordered))


def compact_execution(execution):
    if execution is None:
        return None
    columns = execution['columns'][:32]
    rows, previews = [], 0
    for row in execution['rows'][:30]:
        values = []
        for value in row[:len(columns)]:
            if isinstance(value, str) and len(value) > 160:
                value = {'preview': value[:160], 'truncated': True}
                previews += 1
            elif isinstance(value, bytes):
                value = {'binary_bytes_omitted': len(value)}
            values.append(value)
        rows.append(values)
    result = {k: execution.get(k) for k in ('error', 'truncated', 'row_count', 'rows_saved')}
    result.update(columns=columns, rows=rows, columns_previewed=len(execution['columns']) > len(columns),
                  rows_previewed=len(execution['rows']) > len(rows), cell_previews=previews)
    for key in ('result_kind', 'metadata_source'):
        if key in execution:
            result[key] = execution[key]
    while len(json.dumps(result, ensure_ascii=False)) > 12000 and result['rows']:
        result['rows'].pop()
        result['rows_previewed'] = True
    return result


def previous_attempts(attempts):
    """No reference/gold fields; previews are marked and full records remain on disk."""
    result = []
    for attempt in attempts:
        decision = (attempt.get('answer_generation') or {}).get('decision') or {}
        result.append({
            'top_k': attempt['top_k'], 'selected_views': attempt['selected_views'],
            'sql': (attempt.get('generation') or {}).get('sql'),
            'sql_status': attempt['sql_status'], 'error': attempt.get('error'),
            'execution': compact_execution(attempt.get('execution')),
            'execution_feedback': attempt.get('execution_feedback'),
            'answerability': {k: decision[k] for k in ('answerable', 'answer', 'reason') if k in decision},
        })
    return result


def execution_feedback(error, sql, views, schemas, available_views):
    """Explain known SQLite failures using existing schema names, without LLM calls."""
    if not error:
        return None
    text = str(error)
    lower = text.lower()
    feedback = {'error': text, 'selected_views': list(views)}
    if 'only one sql' in lower:
        feedback.update(kind='multiple_statements', hint='Return exactly one SELECT/WITH statement.')
    elif 'only a select/with' in lower:
        feedback.update(kind='not_select', hint='Object inspection is read-only; return one SELECT/WITH statement.')
    elif 'ambiguous column name:' in lower:
        column = text.split(':', 1)[1].strip().strip('"')
        feedback.update(kind='ambiguous_column', hint='Qualify the column with its View alias.',
                        matching_columns=[{'view': v, 'column': c} for v in views for c in schemas[v]
                                          if c.casefold() == column.casefold()][:8])
    elif 'no such column:' in lower:
        column = text.split(':', 1)[1].split(' - should this')[0].strip().strip('"').split('.', 1)[-1].strip('"')
        fields = [(v, c) for v in views for c in schemas[v]]
        exact = [(v, c) for v, c in fields if c.casefold() == column.casefold()]
        near = set(difflib.get_close_matches(column, list({c for _, c in fields}), n=5, cutoff=0.35))
        matches = exact or [(v, c) for v, c in fields if c in near]
        feedback.update(kind='missing_column', hint='Use a supplied column with its owning View alias; suggestions are names, not verified semantic equivalents.',
                        candidate_columns=[{'view': v, 'column': c} for v, c in matches[:8]])
    elif 'no such table:' in lower or 'prohibited' in lower or 'not authorized' in lower:
        feedback.update(kind='view_scope', hint='Only selected Views provide object data. Other names in the catalogue require selection before access.',
                        available_views=list(available_views))
    elif 'syntax' in lower or 'incomplete input' in lower or 'unrecognized token' in lower:
        feedback.update(kind='syntax', hint='Use double quotes around full column names, including spaces and punctuation; use single quotes for values.')
    else:
        feedback.update(kind='sql_error', hint='Revise this query using the reported error and supplied schema.')
    return feedback
