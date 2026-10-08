import logging
import aiosqlite
from config import TURSO_DATABASE_URL, TURSO_AUTH_TOKEN, DB_FILE

_turso_shared_client = None

class LibsqlCursorWrapper:
    """Turso の結果を aiosqlite カーソル互換で扱うためのラッパー"""
    def __init__(self, result_set):
        self._rs = result_set
        self.rowcount = getattr(result_set, "rows_affected", 0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def fetchone(self):
        if not self._rs or not getattr(self._rs, "rows", None) or len(self._rs.rows) == 0:
            return None
        return tuple(self._rs.rows[0])

    async def fetchall(self):
        if not self._rs or not getattr(self._rs, "rows", None):
            return []
        return [tuple(r) for r in self._rs.rows]


class Database:
    """ローカルSQLiteとTursoクラウドDBを自動判別して透過的に操作するクラス"""
    @classmethod
    def connect(cls):
        return cls()

    async def __aenter__(self):
        global _turso_shared_client
        if TURSO_DATABASE_URL and TURSO_AUTH_TOKEN:
            try:
                import libsql_client
                if _turso_shared_client is None:
                    _turso_shared_client = libsql_client.create_client(
                        url=TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN
                    )
                self._client = _turso_shared_client
                self._is_turso = True
            except Exception as e:
                print(f"Turso接続エラー ({e})。ローカルSQLiteにフォールバック", flush=True)
                self._conn = await aiosqlite.connect(DB_FILE)
                self._is_turso = False
        else:
            self._conn = await aiosqlite.connect(DB_FILE)
            self._is_turso = False
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if not self._is_turso and hasattr(self, "_conn"):
            await self._conn.close()

    async def execute(self, sql, params=()):
        if self._is_turso:
            global _turso_shared_client
            for attempt in range(2):
                try:
                    rs = await self._client.execute(sql, list(params))
                    return LibsqlCursorWrapper(rs)
                except Exception as e:
                    err_msg = str(e).lower()
                    if attempt == 0 and ("disconnected" in err_msg or "closed" in err_msg or "reset" in err_msg):
                        if _turso_shared_client is not None:
                            try:
                                await _turso_shared_client.close()
                            except Exception:
                                pass
                            _turso_shared_client = None
                        import libsql_client
                        _turso_shared_client = libsql_client.create_client(
                            url=TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN
                        )
                        self._client = _turso_shared_client
                        continue
                    print(f"Tursoクエリエラー: {e}", flush=True)
                    raise e
        else:
            return await self._conn.execute(sql, params)

    async def commit(self):
        if not self._is_turso and hasattr(self, "_conn"):
            await self._conn.commit()


async def init_db():
    """全テーブルの作成と既存DBへのマイグレーションを行う"""
    async with Database.connect() as db:
        # サーバーごとの設定テーブル（マルチサーバー対応の中核）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                admin_channel_id INTEGER,
                attendance_channel_id INTEGER,
                room_status_channel_id INTEGER
            )
        """)
        # イベント（出欠）テーブル
        await db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                message_id INTEGER PRIMARY KEY,
                guild_id INTEGER,
                name TEXT,
                date TEXT,
                event_date TEXT
            )
        """)
        # 出欠状況テーブル
        await db.execute("""
            CREATE TABLE IF NOT EXISTS attendances (
                message_id INTEGER,
                user_id INTEGER,
                user_name TEXT,
                status TEXT,
                PRIMARY KEY (message_id, user_id)
            )
        """)
        # 部屋テーブル（guild_id付き複合主キー）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS rooms (
                guild_id INTEGER,
                name TEXT,
                is_open INTEGER,
                last_updated TEXT,
                PRIMARY KEY (guild_id, name)
            )
        """)
        # 部屋パネルメッセージID保存用
        await db.execute("""
            CREATE TABLE IF NOT EXISTS room_panel (
                guild_id INTEGER PRIMARY KEY,
                message_id INTEGER
            )
        """)
        # 既存DBのマイグレーション（カラムがない場合のみ追加）
        for migration_sql in [
            "ALTER TABLE events ADD COLUMN guild_id INTEGER",
            "ALTER TABLE events ADD COLUMN event_date TEXT",
        ]:
            try:
                await db.execute(migration_sql)
            except Exception:
                pass
        await db.commit()


async def get_guild_settings(guild_id: int):
    """指定サーバーの設定をDBから取得する"""
    async with Database.connect() as db:
        async with await db.execute(
            "SELECT admin_channel_id, attendance_channel_id, room_status_channel_id FROM guild_settings WHERE guild_id = ?",
            (guild_id,),
        ) as cursor:
            row = await cursor.fetchone()
    if not row:
        return None
    return {
        "admin_channel_id": row[0],
        "attendance_channel_id": row[1],
        "room_status_channel_id": row[2],
    }
