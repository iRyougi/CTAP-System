import sqlite3
import secrets
import random
import math
import csv
import io
from datetime import datetime, timezone, timedelta

_CST = timezone(timedelta(hours=8))

def now_cst() -> str:
    return datetime.now(_CST).strftime('%Y-%m-%d %H:%M:%S')

DATABASE = 'survey.db'

DEFAULT_CASE_TITLE = '可疑车辆（默认情景）'
DEFAULT_CASE_TEXT = (
    '你是一名战场指挥官。一架无人机发现一辆可疑车辆正在接近己方基地。'
    '情报显示该地区近期发生过多次袭击。'
    '车辆附近可能存在平民活动，但图像不够清晰。'
    '你需要决定是否批准一次空中打击。'
)


def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    return conn


def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS cases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            scenario_text TEXT NOT NULL,
            active INTEGER DEFAULT 1,
            prob_high REAL DEFAULT 0.85,
            prob_low REAL DEFAULT 0.15,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS participants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            group_name TEXT NOT NULL CHECK(group_name IN ('A', 'B', 'C', 'D')),
            case_id INTEGER REFERENCES cases(id),
            used INTEGER DEFAULT 0,
            completed INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            accessed_at TEXT,
            completed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS participant_cases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            participant_code TEXT NOT NULL,
            case_id INTEGER NOT NULL,
            sequence_num INTEGER NOT NULL,
            case_group TEXT,
            ai_prob_condition TEXT,
            page1_answer INTEGER,
            page1_time_seconds REAL,
            page1_timed_out INTEGER DEFAULT 0,
            completed INTEGER DEFAULT 0,
            FOREIGN KEY (case_id) REFERENCES cases(id)
        );

        CREATE TABLE IF NOT EXISTS responses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            participant_code TEXT NOT NULL,
            group_name TEXT NOT NULL,
            case_id INTEGER,
            case_title TEXT,
            sequence_num INTEGER DEFAULT 1,
            approve_strike INTEGER,
            responsibility INTEGER NOT NULL,
            confidence INTEGER NOT NULL,
            ai_influence INTEGER,
            wait_for_intel INTEGER NOT NULL,
            ai_prob_condition INTEGER,
            ai_prob_value REAL,
            page1_timed_out INTEGER DEFAULT 0,
            page1_time_seconds REAL,
            timeout_reason TEXT,
            time_started TEXT,
            time_taken_seconds REAL,
            submitted_at TEXT DEFAULT (datetime('now'))
        );
    ''')

    for migration in [
        'ALTER TABLE participants ADD COLUMN case_id INTEGER',
        'ALTER TABLE responses ADD COLUMN case_id INTEGER',
        'ALTER TABLE responses ADD COLUMN case_title TEXT',
        'ALTER TABLE responses ADD COLUMN sequence_num INTEGER DEFAULT 1',
        'ALTER TABLE responses ADD COLUMN page1_timed_out INTEGER DEFAULT 0',
        'ALTER TABLE responses ADD COLUMN page1_time_seconds REAL',
        'ALTER TABLE responses ADD COLUMN timeout_reason TEXT',
        'ALTER TABLE cases ADD COLUMN prob_high REAL DEFAULT 0.85',
        'ALTER TABLE cases ADD COLUMN prob_low REAL DEFAULT 0.15',
        'ALTER TABLE participant_cases ADD COLUMN case_group TEXT',
        'ALTER TABLE participant_cases ADD COLUMN ai_prob_condition TEXT',
        'ALTER TABLE responses ADD COLUMN ai_prob_condition INTEGER',
        'ALTER TABLE responses ADD COLUMN ai_prob_value REAL',
    ]:
        try:
            conn.execute(migration)
        except Exception:
            pass

    for k, v in [
        ('cases_per_participant', '1'),
        ('collection_open',     '1'),
        ('collection_quota',    ''),
        ('collection_deadline', ''),
        ('survey_language',     'zh'),
    ]:
        conn.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', (k, v))

    if conn.execute('SELECT COUNT(*) FROM cases').fetchone()[0] == 0:
        conn.execute(
            'INSERT INTO cases (title, scenario_text, prob_high, prob_low) VALUES (?, ?, ?, ?)',
            (DEFAULT_CASE_TITLE, DEFAULT_CASE_TEXT, 0.85, 0.15)
        )

    conn.commit()
    conn.close()


# ── Settings ───────────────────────────────────────

def get_setting(key, default=None):
    conn = get_db()
    row = conn.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
    conn.close()
    return row['value'] if row else default

def set_setting(key, value):
    conn = get_db()
    conn.execute(
        'INSERT INTO settings (key, value) VALUES (?, ?) '
        'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
        (key, str(value))
    )
    conn.commit()
    conn.close()


def get_collection_status():
    conn = get_db()
    total = conn.execute('SELECT COUNT(*) FROM responses').fetchone()[0]
    conn.close()

    manual_open  = get_setting('collection_open',     '1') == '1'
    quota_str    = get_setting('collection_quota',    '')
    deadline_str = get_setting('collection_deadline', '')

    base = {'total': total, 'manual_open': manual_open,
            'quota': quota_str, 'deadline': deadline_str}

    if not manual_open:
        return {**base, 'is_open': False, 'reason': 'manual'}

    if quota_str:
        try:
            if total >= int(quota_str):
                return {**base, 'is_open': False, 'reason': 'quota'}
        except ValueError:
            pass

    if deadline_str:
        try:
            deadline = datetime.strptime(deadline_str, '%Y-%m-%dT%H:%M')
            if datetime.now(_CST).replace(tzinfo=None) >= deadline:
                return {**base, 'is_open': False, 'reason': 'deadline'}
        except ValueError:
            pass

    return {**base, 'is_open': True, 'reason': None}


# ── Cases ──────────────────────────────────────────

def get_n_random_cases(n):
    # Each slot independently samples from all active cases (with replacement),
    # so the same case can appear in multiple slots and each draw is unbiased.
    conn = get_db()
    cases = []
    for _ in range(n):
        row = conn.execute(
            'SELECT * FROM cases WHERE active = 1 ORDER BY RANDOM() LIMIT 1'
        ).fetchone()
        if row:
            cases.append(dict(row))
    conn.close()
    return cases

def get_random_case():
    cases = get_n_random_cases(1)
    return cases[0] if cases else None

def get_case(case_id):
    conn = get_db()
    row = conn.execute('SELECT * FROM cases WHERE id = ?', (case_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def get_all_cases():
    conn = get_db()
    rows = conn.execute('SELECT * FROM cases ORDER BY id').fetchall()
    conn.close()
    return [dict(r) for r in rows]

def create_case(title, scenario_text, prob_high=0.85, prob_low=0.15):
    conn = get_db()
    cur = conn.execute(
        'INSERT INTO cases (title, scenario_text, prob_high, prob_low, created_at) VALUES (?, ?, ?, ?, ?)',
        (title, scenario_text, float(prob_high), float(prob_low), now_cst())
    )
    new_id = cur.lastrowid
    conn.commit()
    conn.close()
    return new_id

def update_case(case_id, title, scenario_text, active, prob_high=0.85, prob_low=0.15):
    conn = get_db()
    conn.execute(
        'UPDATE cases SET title=?, scenario_text=?, active=?, prob_high=?, prob_low=? WHERE id=?',
        (title, scenario_text, int(active), float(prob_high), float(prob_low), case_id)
    )
    conn.commit()
    conn.close()

def delete_case(case_id):
    conn = get_db()
    conn.execute('DELETE FROM cases WHERE id=?', (case_id,))
    conn.commit()
    conn.close()

def reset_cases_to_default():
    conn = get_db()
    conn.execute('DELETE FROM cases')
    try:
        conn.execute("DELETE FROM sqlite_sequence WHERE name='cases'")
    except Exception:
        pass
    conn.execute(
        'INSERT INTO cases (title, scenario_text, prob_high, prob_low) VALUES (?, ?, ?, ?)',
        (DEFAULT_CASE_TITLE, DEFAULT_CASE_TEXT, 0.85, 0.15)
    )
    conn.commit()
    conn.close()

def import_cases_csv(content):
    reader = csv.DictReader(io.StringIO(content))
    conn = get_db()
    imported = 0
    errors = []
    for row_num, row in enumerate(reader, 2):
        scenario_text = row.get('scenario_text', '').strip()
        title = row.get('title', '').strip() or f'情景 {row_num - 1}'
        if not scenario_text:
            errors.append(f'第{row_num}行: 缺少 scenario_text 字段')
            continue
        try:
            prob_high = float(row.get('prob_high', 0.85) or 0.85)
            prob_low  = float(row.get('prob_low',  0.15) or 0.15)
        except (ValueError, TypeError):
            prob_high, prob_low = 0.85, 0.15
        conn.execute(
            'INSERT INTO cases (title, scenario_text, prob_high, prob_low) VALUES (?, ?, ?, ?)',
            (title, scenario_text, prob_high, prob_low)
        )
        imported += 1
    conn.commit()
    conn.close()
    return {'imported': imported, 'errors': errors}


# ── Participants ───────────────────────────────────

def assign_balanced_group():
    conn = get_db()
    counts = {'A': 0, 'B': 0, 'C': 0, 'D': 0}
    rows = conn.execute(
        'SELECT group_name, COUNT(*) as cnt FROM participants GROUP BY group_name'
    ).fetchall()
    for row in rows:
        counts[row['group_name']] = row['cnt']
    conn.close()
    min_count = min(counts.values())
    min_groups = [g for g, c in counts.items() if c == min_count]
    return random.choice(min_groups)

def create_participant(code, group, case_id=None):
    conn = get_db()
    conn.execute(
        'INSERT INTO participants (code, group_name, case_id, created_at) VALUES (?, ?, ?, ?)',
        (code, group, case_id, now_cst())
    )
    conn.commit()
    conn.close()

def get_participant(code):
    conn = get_db()
    row = conn.execute('SELECT * FROM participants WHERE code = ?', (code,)).fetchone()
    conn.close()
    return dict(row) if row else None

def mark_participant_used(code):
    conn = get_db()
    conn.execute(
        'UPDATE participants SET used = 1, accessed_at = ? WHERE code = ? AND used = 0',
        (now_cst(), code)
    )
    conn.commit()
    conn.close()

def mark_participant_completed(code):
    conn = get_db()
    conn.execute(
        'UPDATE participants SET completed = 1, completed_at = ? WHERE code = ?',
        (now_cst(), code)
    )
    conn.commit()
    conn.close()


# ── Participant Cases (multi-case tracking) ────────

def assign_cases_to_participant(code, n):
    cases = get_n_random_cases(n)
    # Build balanced high/low AI probability conditions (mixed across N cases)
    if n == 1:
        conditions = [random.choice(['high', 'low'])]
    else:
        half = math.ceil(n / 2)
        conditions = ['high'] * half + ['low'] * (n - half)
        random.shuffle(conditions)
    conn = get_db()

    # Globally balanced case_group assignment — pick least-populated group each time,
    # updating local counts so multiple slots in the same batch are balanced too.
    counts = {'A': 0, 'B': 0, 'C': 0, 'D': 0}
    for row in conn.execute(
        'SELECT case_group, COUNT(*) as cnt FROM participant_cases '
        'WHERE case_group IS NOT NULL GROUP BY case_group'
    ).fetchall():
        if row['case_group'] in counts:
            counts[row['case_group']] = row['cnt']

    for i, (case, cond) in enumerate(zip(cases, conditions), 1):
        min_cnt = min(counts.values())
        candidates = [g for g, c in counts.items() if c == min_cnt]
        case_group = random.choice(candidates)
        counts[case_group] += 1
        conn.execute(
            'INSERT INTO participant_cases '
            '(participant_code, case_id, sequence_num, case_group, ai_prob_condition) '
            'VALUES (?, ?, ?, ?, ?)',
            (code, case['id'], i, case_group, cond)
        )
    conn.commit()
    conn.close()
    return cases

def get_current_participant_case(code):
    conn = get_db()
    row = conn.execute(
        'SELECT pc.*, c.title, c.scenario_text, c.prob_high, c.prob_low '
        'FROM participant_cases pc JOIN cases c ON pc.case_id = c.id '
        'WHERE pc.participant_code = ? AND pc.completed = 0 ORDER BY pc.sequence_num LIMIT 1',
        (code,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None

def save_page1_answer(code, sequence_num, page1_answer, page1_time_seconds, page1_timed_out):
    conn = get_db()
    conn.execute(
        'UPDATE participant_cases SET page1_answer=?, page1_time_seconds=?, page1_timed_out=? '
        'WHERE participant_code=? AND sequence_num=?',
        (page1_answer, page1_time_seconds, int(page1_timed_out), code, sequence_num)
    )
    conn.commit()
    conn.close()

def mark_participant_case_completed(code, sequence_num):
    conn = get_db()
    conn.execute(
        'UPDATE participant_cases SET completed=1 WHERE participant_code=? AND sequence_num=?',
        (code, sequence_num)
    )
    conn.commit()
    conn.close()

def count_participant_cases(code):
    conn = get_db()
    total = conn.execute(
        'SELECT COUNT(*) FROM participant_cases WHERE participant_code=?', (code,)
    ).fetchone()[0]
    done = conn.execute(
        'SELECT COUNT(*) FROM participant_cases WHERE participant_code=? AND completed=1', (code,)
    ).fetchone()[0]
    conn.close()
    return total, done


# ── Responses ──────────────────────────────────────

def save_response(code, group, approve_strike, responsibility, confidence,
                  ai_influence, wait_for_intel, time_taken,
                  case_id=None, case_title=None, sequence_num=1,
                  page1_timed_out=0, page1_time_seconds=None, timeout_reason=None,
                  ai_prob_condition=None, ai_prob_value=None):
    conn = get_db()
    conn.execute('''
        INSERT INTO responses
        (participant_code, group_name, case_id, case_title, sequence_num,
         approve_strike, responsibility, confidence,
         ai_influence, wait_for_intel, ai_prob_condition, ai_prob_value,
         page1_timed_out, page1_time_seconds,
         timeout_reason, time_taken_seconds, submitted_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (code, group, case_id, case_title, sequence_num,
          approve_strike, responsibility, confidence,
          ai_influence, wait_for_intel, ai_prob_condition, ai_prob_value,
          int(page1_timed_out), page1_time_seconds,
          timeout_reason, time_taken, now_cst()))
    conn.commit()
    conn.close()

def clear_all_data():
    conn = get_db()
    conn.execute('DELETE FROM responses')
    conn.execute('DELETE FROM participants')
    conn.execute('DELETE FROM participant_cases')
    conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('responses','participants','participant_cases')")
    conn.commit()
    conn.close()

def get_all_responses():
    conn = get_db()
    rows = conn.execute('SELECT * FROM responses ORDER BY submitted_at DESC').fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_stats():
    conn = get_db()
    total = conn.execute('SELECT COUNT(*) FROM responses').fetchone()[0]
    by_group = {}
    for group in ['A', 'B', 'C', 'D']:
        rows = conn.execute(
            'SELECT approve_strike, responsibility, confidence, ai_influence '
            'FROM responses WHERE group_name = ?', (group,)
        ).fetchall()
        count = len(rows)
        if count > 0:
            approve_count = sum(1 for r in rows if r['approve_strike'])
            avg_resp = sum(r['responsibility'] for r in rows) / count
            avg_conf = sum(r['confidence'] for r in rows) / count
            ai_rows = [r for r in rows if r['ai_influence'] is not None]
            avg_ai = sum(r['ai_influence'] for r in ai_rows) / len(ai_rows) if ai_rows else None
        else:
            approve_count = 0; avg_resp = None; avg_conf = None; avg_ai = None
        by_group[group] = {
            'count': count, 'approve_count': approve_count,
            'approve_rate': round(approve_count / count, 4) if count > 0 else 0,
            'avg_responsibility': round(avg_resp, 2) if avg_resp is not None else None,
            'avg_confidence': round(avg_conf, 2) if avg_conf is not None else None,
            'avg_ai_influence': round(avg_ai, 2) if avg_ai is not None else None,
        }
    participant_counts = {'A': 0, 'B': 0, 'C': 0, 'D': 0}
    rows = conn.execute(
        'SELECT group_name, COUNT(*) as cnt FROM participants GROUP BY group_name'
    ).fetchall()
    for row in rows:
        participant_counts[row['group_name']] = row['cnt']
    conn.close()
    return {'total': total, 'by_group': by_group, 'participant_counts': participant_counts}


def generate_codes_batch(count):
    groups = ['A', 'B', 'C', 'D']
    full_blocks = count // 4
    remainder = count % 4
    assignment_list = groups * full_blocks
    if remainder > 0:
        assignment_list += random.sample(groups, remainder)
    random.shuffle(assignment_list)
    conn = get_db()
    generated = []
    for group in assignment_list:
        code = secrets.token_urlsafe(8)
        while conn.execute('SELECT id FROM participants WHERE code = ?', (code,)).fetchone():
            code = secrets.token_urlsafe(8)
        conn.execute('INSERT INTO participants (code, group_name) VALUES (?, ?)', (code, group))
        generated.append((code, group))
    conn.commit()
    conn.close()
    return generated


def import_participants_csv(content):
    reader = csv.DictReader(io.StringIO(content))
    conn = get_db()
    imported = 0
    errors = []
    for row_num, row in enumerate(reader, 2):
        code = row.get('code', '').strip()
        group = row.get('group', '').strip().upper()
        if not code:
            errors.append(f'第{row_num}行: 缺少 code 字段'); continue
        if group not in ['A', 'B', 'C', 'D']:
            errors.append(f'第{row_num}行: 无效组别 "{group}"，必须为 A/B/C/D'); continue
        if conn.execute('SELECT id FROM participants WHERE code = ?', (code,)).fetchone():
            errors.append(f'第{row_num}行: 代码 "{code}" 已存在，已跳过'); continue
        conn.execute('INSERT INTO participants (code, group_name) VALUES (?, ?)', (code, group))
        imported += 1
    conn.commit()
    conn.close()
    return {'imported': imported, 'errors': errors}
