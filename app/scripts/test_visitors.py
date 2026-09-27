"""M1 storage/migration tests: synthetic account databases; no login or provider calls."""
from __future__ import annotations
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['CARME_LOAD_ENV'] = '0'
from carme.store import Store, SCHEMA, M4_SCHEMA

# A syntactically valid verifier, deliberately not used to authenticate anyone.
HASH = 'scrypt$131072$8$1$' + 'a' * 32 + '$' + 'b' * 64
NEW_HASH = 'scrypt$131072$8$1$' + 'c' * 32 + '$' + 'd' * 64


def legacy_database(path):
    """Pre-M1 schema, with explicit rows exercising all three sender roles and requests."""
    c = sqlite3.connect(path)
    c.executescript(SCHEMA + M4_SCHEMA)
    for table, fields in {
        'attachments': {'sha256': "TEXT NOT NULL DEFAULT ''", 'provenance': "TEXT NOT NULL DEFAULT ''"},
        'tasks': {'conversation_id': "TEXT NOT NULL DEFAULT ''"},
        'conversations': {'members_revision': 'INTEGER NOT NULL DEFAULT 0', 'pinned_at': 'REAL NOT NULL DEFAULT 0',
                          'folder': "TEXT NOT NULL DEFAULT ''", 'hidden': 'INTEGER NOT NULL DEFAULT 0',
                          'deleted_at': 'REAL NOT NULL DEFAULT 0', 'unread': 'INTEGER NOT NULL DEFAULT 0'},
        'conversation_messages': {'model': "TEXT NOT NULL DEFAULT ''", 'provider': "TEXT NOT NULL DEFAULT ''", 'status': "TEXT NOT NULL DEFAULT 'done'"},
    }.items():
        for name, definition in fields.items():
            c.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
    c.execute("INSERT INTO schema_versions VALUES('m4_memory',1)")
    c.execute("INSERT INTO conversations(id,title,agent_ids,kind,created_at,updated_at) VALUES('c_old','preserve','[\"a\",\"b\"]','group',1,2)")
    c.execute("INSERT INTO tasks(id,agent_id,goal,status,created_at,conversation_id) VALUES('t_old','a','KEEP GOAL','done',1,'c_old')")
    for rowid, mid, role, created in [(5, 'm_user', 'user', 20), (12, 'm_bot', 'assistant', 10), (16, 'm_system', 'system', 20)]:
        c.execute('INSERT INTO conversation_messages(rowid,id,conversation_id,agent_id,role,content,task_id,created_at) VALUES(?,?,?,?,?,?,?,?)',
                  (rowid, mid, 'c_old', 'a', role, 'KEEP '+mid, 't_old', created))
    c.execute("INSERT INTO conversation_requests(rowid,conversation_id,request_id,task_id,message_id) VALUES(9,'c_old','retry','t_old','m_user')")
    c.execute("INSERT INTO conversation_deliveries VALUES('c_old','m_user','a','t_old')")
    c.execute("INSERT INTO conversation_summaries VALUES('c_old','KEEP SUMMARY',16,'fixture',2)")
    c.execute("INSERT INTO attachments(id,conversation_id,message_id,name,mime,size,created_at) VALUES('f_old','c_old','m_user','KEEP.txt','text/plain',4,1)")
    c.execute("INSERT INTO memory VALUES('a','key','KEEP MEMORY',1,1)")
    c.execute("INSERT INTO memory_versions(scope,scope_id,key,version,value,actor,created_at) VALUES('bot','a','key',1,'KEEP MEMORY','legacy',1)")
    c.execute("INSERT INTO task_operations(id,task_id,action_digest,tool,status,created_at,updated_at) VALUES('op_old','t_old','opaque','shell','pending',1,1)")
    c.commit()
    c.close()


def old_snapshot(path):
    with closing(sqlite3.connect(path)) as c:
        result = {}
        for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            if name.startswith('sqlite_'):
                continue
            fields = [r[1] for r in c.execute(f'PRAGMA table_info({name})')]
            result[name] = (fields, c.execute(f'SELECT rowid,{",".join(fields)} FROM {name} ORDER BY rowid').fetchall())
        return result


class Visitors(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='carme-visitor-m1-')
        self.root = Path(self.tmp.name).resolve()
        self.store = Store(self.root/'account.db')
        self.cid = self.store.create_conversation(['a','b'])['id']

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def revision(self, cid=None):
        return (self.store.get_conversation(cid or self.cid) or {}).get('access_revision', 0)

    def invite(self, name='Guest', cid=None):
        cid = cid or self.cid
        return self.store.create_visitor(cid, name, HASH, actor_key='owner', expected_revision=self.revision(cid))

    def change(self, visitor, action, **kwargs):
        return self.store.update_visitor(self.cid, visitor['id'], action, actor_key='owner',
                                         expected_revision=self.revision(), **kwargs)

    @staticmethod
    def actor(v):
        return f"visitor:{v['id']}:{v['membership_version']}"

    def seed_session(self, v):
        self.store._write('INSERT INTO visitor_sessions VALUES(?,?,?,?,?,?,?,?,?)',
                          ('fixture-session','1'*64,v['id'],v['credential_version'],v['membership_version'],'2'*64,'3'*64,1,9999999999))

    def test_legacy_migration_preserves_every_old_column_and_rowid(self):
        old = self.root/'old.db';copy = self.root/'candidate.db'
        legacy_database(old)
        # SQLite backup produces a detached migration candidate; source stays untouched.
        with closing(sqlite3.connect(old)) as src, closing(sqlite3.connect(copy)) as dst:
            src.backup(dst)
        before = old_snapshot(old)
        migrated = Store(copy)
        try:
            for table,(fields,rows) in before.items():
                actual = migrated._conn.execute(f'SELECT rowid,{",".join(fields)} FROM {table} ORDER BY rowid').fetchall()
                actual = [tuple(r) for r in actual]
                if table == 'schema_versions':
                    actual = [r for r in actual if r[1] != 'visitor_m1']
                self.assertEqual(actual,rows,table)
            rows = migrated._query('SELECT rowid,id,publication_seq,sender_kind,sender_id FROM conversation_messages ORDER BY rowid')
            self.assertEqual([(r['rowid'],r['publication_seq'],r['sender_kind'],r['sender_id']) for r in rows],
                             [(5,1,'owner','owner'),(12,2,'bot','a'),(16,3,'system','system')])
            self.assertEqual(migrated.get_conversation_request('c_old','retry')['message_id'],'m_user')
            self.assertEqual(migrated.get_conversation('c_old')['next_publication_seq'],3)
            self.assertEqual(migrated._conn.execute('PRAGMA integrity_check').fetchone()[0],'ok')
        finally:
            migrated.close()
        self.assertEqual(old_snapshot(old),before)
        first = old_snapshot(copy)
        with_store = Store(copy);with_store.close()
        self.assertEqual(old_snapshot(copy),first)

    def test_migration_failure_rolls_back_and_retry_succeeds(self):
        path=self.root/'rollback.db';legacy_database(path)
        c=sqlite3.connect(path)
        c.execute('CREATE TABLE visitor_login_attempts (sentinel TEXT)');c.commit();c.close()
        with self.assertRaises(sqlite3.OperationalError):
            Store(path)
        with closing(sqlite3.connect(path)) as c:
            self.assertNotIn('access_revision',[r[1] for r in c.execute('PRAGMA table_info(conversations)')])
            self.assertNotIn('actor_key',[r[1] for r in c.execute('PRAGMA table_info(conversation_requests)')])
            self.assertIsNone(c.execute("SELECT 1 FROM schema_versions WHERE name='visitor_m1'").fetchone())
            self.assertEqual(c.execute('SELECT count(*) FROM conversation_messages').fetchone()[0],3)
            c.execute('DROP TABLE visitor_login_attempts');c.commit()
        migrated=Store(path);self.assertEqual(migrated.get_conversation('c_old')['next_publication_seq'],3);migrated.close()

    def test_concurrent_capacity_across_connections(self):
        stores=[Store(self.store.path) for _ in range(8)]
        barrier=threading.Barrier(len(stores))
        def create(st):
            barrier.wait()
            for _ in range(20):
                rev=st.get_conversation(self.cid)['access_revision']
                try:
                    return st.create_visitor(self.cid,'Concurrent',HASH,actor_key='owner',expected_revision=rev)
                except ValueError as e:
                    if str(e)=='visitor_revision_conflict':continue
                    return str(e)
            self.fail('revision retry budget exhausted')
        try:
            with ThreadPoolExecutor(max_workers=8) as pool:rows=list(pool.map(create,stores))
            self.assertEqual(sum(isinstance(r,dict) for r in rows),3)
            self.assertEqual(rows.count('visitor_capacity_exceeded'),5)
            self.assertEqual(self.revision(),3)
        finally:
            for st in stores:st.close()

    def test_storage_owner_and_account_group_guards(self):
        v=self.invite();other=Store(self.root/'other-account.db')
        direct=self.store.create_conversation(['a'])['id']
        try:
            for cid in (direct,'missing'):
                with self.assertRaisesRegex(ValueError,'group_denied'):
                    self.invite(cid=cid)
            with self.assertRaisesRegex(ValueError,'owner_required'):
                self.store.create_visitor(self.cid,'No',HASH,actor_key=self.actor(v),expected_revision=self.revision())
            with self.assertRaisesRegex(ValueError,'owner_required'):
                self.store.update_visitor(self.cid,v['id'],'remove',actor_key=self.actor(v),expected_revision=self.revision())
            self.assertIsNone(other.get_visitor(self.cid,v['id']))
            with self.assertRaisesRegex(ValueError,'group_denied'):
                other.create_visitor(self.cid,'No',HASH,actor_key='owner',expected_revision=0)
            cid2=self.store.create_conversation(['a','b'])['id']
            with self.assertRaisesRegex(ValueError,'identity_denied'):
                self.store.update_visitor(cid2,v['id'],'remove',actor_key='owner',expected_revision=0)
            with self.assertRaisesRegex(ValueError,'membership_revoked'):
                self.store.create_conversation_turn(cid2,'a','No','req',actor_key=self.actor(v))
            with self.assertRaisesRegex(sqlite3.IntegrityError,'identity_immutable'):
                self.store._write('UPDATE visitors SET conversation_id=? WHERE id=?',(cid2,v['id']))
        finally:other.close()

    def test_password_verifiers_only_and_safe_projection(self):
        for bad in ('plaintext password','',HASH+'x',None):
            with self.assertRaisesRegex(ValueError,'password_hash_required'):
                self.store.create_visitor(self.cid,'No',bad,actor_key='owner',expected_revision=self.revision())
        v=self.invite()
        self.assertNotIn('password_hash',v)
        self.assertEqual(self.store._query_one('SELECT password_hash FROM visitors WHERE id=?',(v['id'],))['password_hash'],HASH)
        for action in ('reset_password','reinvite'):
            with self.assertRaisesRegex(ValueError,'password_hash_required'):
                self.change(v,action,password_hash='plaintext')

    def test_reset_remove_and_reinvite_versions_and_watermarks(self):
        self.store.add_conversation_message(self.cid,'a','assistant','old')
        v=self.invite();self.assertEqual(v['visible_after_seq'],1)
        self.seed_session(v)
        current=self.revision()
        v=self.change(v,'reset_password',password_hash=NEW_HASH)
        self.assertEqual(self.revision(),current)
        self.assertEqual((v['credential_version'],v['membership_version'],v['visible_after_seq']),(2,1,1))
        self.assertEqual(self.store._query('SELECT * FROM visitor_sessions'),[])
        self.seed_session(v)
        v=self.change(v,'history',allow_history=True);self.assertEqual(v['allow_history'],1)
        removed=self.change(v,'remove');self.assertFalse(removed['enabled'])
        self.assertEqual(self.store._query('SELECT * FROM visitor_sessions'),[])
        self.store.add_conversation_message(self.cid,'a','assistant','later')
        again=self.change(removed,'reinvite',password_hash=HASH)
        self.assertEqual((again['username'],again['id']),(v['username'],v['id']))
        self.assertEqual((again['visible_after_seq'],again['allow_history'],again['membership_version']),(2,0,3))
        with self.assertRaisesRegex(ValueError,'membership_revoked'):
            self.store.get_conversation_request(self.cid,'old',actor_key=self.actor(v))
        self.assertIsNone(self.store.get_conversation_request(self.cid,'old',actor_key=self.actor(again)))

    def test_reinvite_capacity_and_revision_conflicts(self):
        a=self.invite();removed=self.change(a,'remove')
        for _ in range(3):self.invite()
        with self.assertRaisesRegex(ValueError,'capacity_exceeded'):
            self.change(removed,'reinvite',password_hash=HASH)
        with self.assertRaisesRegex(ValueError,'revision_conflict'):
            self.store.create_visitor(self.cid,'stale',HASH,actor_key='owner',expected_revision=0)
        self.assertFalse(self.store.get_visitor(self.cid,a['id'])['enabled'])
        current=self.revision()
        with self.assertRaisesRegex(ValueError,'history_invalid'):
            self.change(removed,'history',allow_history='yes')
        self.assertEqual(self.revision(),current)

    def test_publication_counter_stream_update_deletion_and_clock(self):
        mid=self.store.add_conversation_message(self.cid,'a','assistant','first',status='streaming')
        v=self.invite();self.assertEqual(v['visible_after_seq'],1)
        self.store.add_conversation_message(self.cid,'a','assistant','more',message_id=mid,status='done')
        self.assertEqual(self.store.get_conversation(self.cid)['next_publication_seq'],1)
        self.store._write('DELETE FROM conversation_messages WHERE id=?',(mid,))
        with patch('carme.store.time.time',return_value=-100):
            new=self.store.add_conversation_message(self.cid,'a','user','new')
        row=self.store._query_one('SELECT * FROM conversation_messages WHERE id=?',(new,))
        self.assertEqual(row['publication_seq'],2)
        self.assertGreater(row['publication_seq'],v['visible_after_seq'])
        self.assertEqual((row['sender_kind'],row['sender_id']),('owner','owner'))
        with self.assertRaisesRegex(sqlite3.IntegrityError,'identity_immutable'):
            self.store._write('UPDATE conversation_messages SET publication_seq=999 WHERE id=?',(new,))

    def test_insert_sequence_is_transactional_in_all_writers(self):
        turn=self.store.create_conversation_turn(self.cid,'a','owner','owner-req')
        self.assertEqual(self.store.list_conversation_messages(self.cid)[0]['publication_seq'],1)
        with self.assertRaisesRegex(RuntimeError,'rollback'):
            with self.store.transaction():
                self.store.add_conversation_message(self.cid,'a','assistant','rolled back')
                raise RuntimeError('rollback')
        self.assertEqual(self.store.get_conversation(self.cid)['next_publication_seq'],1)
        self.assertEqual(len(self.store.list_conversation_messages(self.cid)),1)
        imported=self.store.import_conversation({'agent_ids':['a','b'],'updated_at':5,'messages':[
            {'role':'user','content':'u','sender_kind':'visitor','sender_id':'forged','publication_seq':999},
            {'role':'assistant','content':'b','agent_id':'b'}, {'role':'system','content':'s'}]})
        rows=self.store.list_conversation_messages(imported['id'])
        self.assertEqual([r['publication_seq'] for r in rows],[1,2,3])
        self.assertEqual([(r['sender_kind'],r['sender_id']) for r in rows],[('owner','owner'),('bot','b'),('system','system')])
        self.assertEqual(imported['updated_at'],5)
        self.assertEqual(self.store.get_conversation_request(self.cid,'owner-req')['task_id'],turn['task_id'])

    def test_actor_idempotency_and_sender_are_separate_from_bot(self):
        a,b=self.invite('A'),self.invite('B')
        replies=[self.store.create_conversation_turn(self.cid,'a','same','same-request',actor_key=actor)
                 for actor in ('owner',self.actor(a),self.actor(b))]
        self.assertEqual(len({r['task_id'] for r in replies}),3)
        repeat=self.store.create_conversation_turn(self.cid,'a','same','same-request',actor_key=self.actor(a))
        self.assertFalse(repeat['created']);self.assertEqual(repeat['task_id'],replies[1]['task_id'])
        rows=self.store.list_conversation_messages(self.cid)
        self.assertEqual([r['agent_id'] for r in rows],['a']*3)
        self.assertEqual([(r['sender_kind'],r['sender_id']) for r in rows],[('owner','owner'),('visitor',a['id']),('visitor',b['id'])])
        with self.assertRaisesRegex(ValueError,'另一条消息'):
            self.store.create_conversation_turn(self.cid,'a','different','same-request',actor_key=self.actor(a))
        for kwargs in ({'active_task_id':replies[0]['task_id']},{'attachment_ids':['anything']}):
            with self.assertRaisesRegex(ValueError,'steering_or_upload_denied'):
                self.store.create_conversation_turn(self.cid,'a','no','new',actor_key=self.actor(a),**kwargs)
        removed=self.change(a,'remove')
        with self.assertRaisesRegex(ValueError,'membership_revoked'):
            self.store.get_conversation_request(self.cid,'same-request',actor_key=self.actor(a))
        again=self.change(removed,'reinvite',password_hash=HASH)
        self.assertIsNone(self.store.get_conversation_request(self.cid,'same-request',actor_key=self.actor(again)))

    def test_message_identity_cannot_be_overwritten_across_groups_or_senders(self):
        a,b=self.invite('A'),self.invite('B')
        mid=self.store.add_conversation_message(self.cid,'','user','visitor',actor_key=self.actor(a))
        cid2=self.store.create_conversation(['a','b'])['id']
        for cid,aid,role,actor in ((cid2,'','user','owner'),(self.cid,'','user',self.actor(b)),(self.cid,'a','assistant','owner')):
            with self.assertRaisesRegex(ValueError,'identity_immutable'):
                self.store.add_conversation_message(cid,aid,role,'overwrite',message_id=mid,actor_key=actor)
        self.assertEqual(self.store.list_conversation_messages(self.cid)[0]['content'],'visitor')
        with self.assertRaisesRegex(ValueError,'actor_denied'):
            self.store.add_conversation_message(self.cid,'a','assistant','impersonation',actor_key=self.actor(a))

    def test_concurrent_publications_and_invites_share_one_order(self):
        stores=[Store(self.store.path) for _ in range(6)]
        barrier=threading.Barrier(6)
        def write(st):
            barrier.wait()
            return st.add_conversation_message(self.cid,'a','assistant','concurrent')
        try:
            with ThreadPoolExecutor(max_workers=6) as pool:
                list(pool.map(write,stores))
            self.assertEqual(sorted(r['publication_seq'] for r in self.store.list_conversation_messages(self.cid)),list(range(1,7)))
            v=self.invite()
            self.assertEqual(v['visible_after_seq'],6)
            self.assertEqual(self.store.get_conversation(self.cid)['next_publication_seq'],6)
        finally:
            for st in stores:st.close()

    def test_soft_delete_restore_invalidates_old_membership_and_session(self):
        v=self.invite();self.seed_session(v)
        original=self.revision()
        self.store.update_conversation(self.cid,{'deleted':True})
        self.assertEqual(self.store._query('SELECT * FROM visitor_sessions'),[])
        self.store.update_conversation(self.cid,{'deleted':False})
        self.assertEqual(self.revision(),original+2)
        with self.assertRaisesRegex(ValueError,'membership_revoked'):
            self.store.get_conversation_request(self.cid,'x',actor_key=self.actor(v))
        fresh=self.store.get_visitor(self.cid,v['id'])
        self.assertEqual(fresh['membership_version'],v['membership_version']+2)
        self.assertEqual(fresh['visible_after_seq'],v['visible_after_seq'])

    def test_session_schema_rejects_plaintext_and_noop_history_does_not_churn(self):
        v=self.invite()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store._write('INSERT INTO visitor_sessions VALUES(?,?,?,?,?,?,?,?,?)',
                ('invalid','plaintext',v['id'],1,1,'2'*64,'3'*64,1,2))
        self.assertEqual(self.store._query('SELECT * FROM visitor_sessions'),[])
        self.seed_session(v)
        revision=self.revision()
        unchanged=self.change(v,'history',allow_history=False)
        self.assertEqual(unchanged,v)
        self.assertEqual(self.revision(),revision)
        self.assertEqual(len(self.store._query('SELECT * FROM visitor_sessions')),1)

    def test_ai_members_revision_and_purge_tombstones(self):
        v=self.invite();self.seed_session(v)
        before=self.revision()
        g=self.store.update_conversation_members(self.cid,['a'],0)
        self.assertEqual((g['members_revision'],g['access_revision']),(1,before+1))
        self.store.update_conversation(self.cid,{'deleted':True})
        with self.assertRaisesRegex(ValueError,'membership_revoked'):
            self.store.get_conversation_request(self.cid,'x',actor_key=self.actor(v))
        self.store.purge_conversations([self.cid])
        self.assertIsNone(self.store.get_conversation(self.cid))
        self.assertFalse(self.store.get_visitor(self.cid,v['id'])['enabled'])
        self.assertEqual(self.store._query('SELECT * FROM visitor_sessions'),[])


if __name__=='__main__':
    unittest.main(verbosity=2)
