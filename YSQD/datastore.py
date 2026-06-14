import sqlite3


INVALID_STATUS_PLACEHOLDERS = {"???", "？？？"}


class DataStore:
    def __init__(self, db_path, table_name, columns, status_col, extra_columns=None):
        self._db = sqlite3.connect(db_path)
        self._db.row_factory = sqlite3.Row
        self._table = table_name
        self._columns = columns
        self._status_col = status_col
        self._extra_columns = extra_columns or []
        self._init_db()

    def _init_db(self):
        all_cols = [name for name, _ in self._columns] + list(self._extra_columns)
        cols_sql = ", ".join([f"{name} TEXT" for name in all_cols])
        
        # 构建CREATE TABLE语句，处理status_col为None的情况
        create_sql = f"""
        CREATE TABLE IF NOT EXISTS {self._table} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            {cols_sql}
        """
        
        # 如果status_col不为None，添加该列
        if self._status_col:
            create_sql = create_sql.rstrip()[:-1] + f",\n                {self._status_col} TEXT DEFAULT '未报'\n            )"
        else:
            create_sql = create_sql.rstrip()[:-1] + "\n            )"
        
        self._db.execute(create_sql)
        self._ensure_columns()
        self._repair_corrupted_status_values()
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS template_options (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL
            )
            """
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS server_options (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL
            )
            """
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS main_category_options (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                parent_id INTEGER DEFAULT 0,
                is_main_menu INTEGER DEFAULT 0,
                slug TEXT
            )
            """
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS reported_domains (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                serverip TEXT,
                template TEXT,
                category TEXT,
                reported_at TEXT,
                response TEXT
            )
            """
        )
        self._ensure_indexes()
        self._db.commit()
        
        self._init_default_settings()

    def _ensure_indexes(self):
        try:
            self._db.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{self._table}_domain ON {self._table}(domain)"
            )
            self._db.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{self._table}_category ON {self._table}(category)"
            )
            self._db.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{self._table}_report_status ON {self._table}(report_status)"
            )
        except Exception:
            pass

    def _init_default_settings(self):
        cursor = self._db.cursor()
        default_settings = {
            "report_username": "liwei",
            "report_password": "123456",
            "erp_username": "linwei",
            "erp_password": "linwei123",
        }
        for key, value in default_settings.items():
            cursor.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value)
            )
        self._db.commit()

    def _ensure_columns(self):
        cur = self._db.execute(f"PRAGMA table_info({self._table})")
        existing = {row[1] for row in cur.fetchall()}
        for name, _ in self._columns:
            if name in existing:
                continue
            self._db.execute(f"ALTER TABLE {self._table} ADD COLUMN {name} TEXT")
        for name in self._extra_columns:
            if name in existing:
                continue
            self._db.execute(f"ALTER TABLE {self._table} ADD COLUMN {name} TEXT")
        if self._status_col and self._status_col not in existing:
            self._db.execute(
                f"ALTER TABLE {self._table} ADD COLUMN {self._status_col} TEXT DEFAULT '未报'"
            )
        if "data_source_id" in existing and {"main_data_source_id", "extra_data_source_id"}.issubset(existing):
            self._db.execute(
                f"""
                UPDATE {self._table}
                SET main_data_source_id = COALESCE(main_data_source_id, data_source_id),
                    extra_data_source_id = COALESCE(extra_data_source_id, '')
                WHERE (main_data_source_id IS NULL OR main_data_source_id = '')
                """
            )
        self._db.commit()

    def _repair_corrupted_status_values(self):
        cur = self._db.execute(f"PRAGMA table_info({self._table})")
        existing = {row[1] for row in cur.fetchall()}
        if {"main_data_status", "main_data_time"}.issubset(existing):
            placeholders = ", ".join(["?"] * len(INVALID_STATUS_PLACEHOLDERS))
            self._db.execute(
                f"""
                UPDATE {self._table}
                SET main_data_status = CASE
                    WHEN COALESCE(main_data_time, '') <> '' THEN '已上传'
                    ELSE '未上传'
                END
                WHERE main_data_status IN ({placeholders})
                """,
                tuple(INVALID_STATUS_PLACEHOLDERS),
            )
            self._db.commit()

    def _normalize_field_values(self, values, existing_row=None):
        normalized = {key: value for key, value in values.items()}
        main_status = normalized.get("main_data_status")
        if isinstance(main_status, str) and main_status.strip() in INVALID_STATUS_PLACEHOLDERS:
            time_value = normalized.get("main_data_time")
            if time_value is None and existing_row is not None:
                time_value = existing_row["main_data_time"]
            normalized["main_data_status"] = "已上传" if (time_value or "").strip() else "未上传"
        return normalized

    def query_rows(self, keyword):
        if not keyword:
            cur = self._db.execute(f"SELECT * FROM {self._table} ORDER BY id DESC")
            return cur.fetchall()

        like = f"%{keyword}%"
        where = " OR ".join([f"{name} LIKE ?" for name, _ in self._columns])
        cur = self._db.execute(
            f"SELECT * FROM {self._table} WHERE {where} ORDER BY id DESC",
            [like] * len(self._columns),
        )
        return cur.fetchall()

    def add_row(self, values=None, commit=True):
        cursor = self._db.cursor()
        values = {key: value for key, value in (values or {}).items() if value is not None}
        if values:
            columns = list(values.keys())
            cursor.execute(
                f"""
                INSERT INTO {self._table} ({",".join(columns)})
                VALUES ({",".join(["?"] * len(columns))})
                """,
                [values[name] for name in columns],
            )
        else:
            columns = [n for n, _ in self._columns]
            cursor.execute(
                f"""
                INSERT INTO {self._table} ({",".join(columns)})
                VALUES ({",".join(["?"] * len(columns))})
                """,
                [""] * len(columns),
            )
        if commit:
            self._db.commit()
        return cursor.lastrowid

    def delete_rows(self, ids):
        self._db.executemany(f"DELETE FROM {self._table} WHERE id = ?", [(i,) for i in ids])
        self._db.commit()

    def update_cell(self, row_id, col_name, value, commit=True):
        if col_name == "main_data_status" and isinstance(value, str) and value.strip() in INVALID_STATUS_PLACEHOLDERS:
            row = self.get_row(row_id)
            time_value = row["main_data_time"] if row else ""
            value = "已上传" if (time_value or "").strip() else "未上传"
        self._db.execute(
            f"UPDATE {self._table} SET {col_name} = ? WHERE id = ?",
            (value, row_id),
        )
        if commit:
            self._db.commit()

    def update_fields(self, row_id, values, commit=True):
        values = {key: value for key, value in values.items() if key}
        if not values:
            return
        existing_row = self.get_row(row_id)
        values = self._normalize_field_values(values, existing_row=existing_row)
        assignments = ", ".join([f"{name} = ?" for name in values])
        params = [values[name] for name in values] + [row_id]
        self._db.execute(
            f"UPDATE {self._table} SET {assignments} WHERE id = ?",
            params,
        )
        if commit:
            self._db.commit()

    def commit(self):
        self._db.commit()

    def get_row(self, row_id):
        cur = self._db.execute(f"SELECT * FROM {self._table} WHERE id = ?", (row_id,))
        return cur.fetchone()

    def get_rows_by_field(self, field_name, value):
        allowed_fields = {name for name, _ in self._columns}
        allowed_fields.update(self._extra_columns)
        if self._status_col:
            allowed_fields.add(self._status_col)
        if field_name not in allowed_fields:
            raise ValueError(f"Unsupported field lookup: {field_name}")
        cur = self._db.execute(
            f"SELECT * FROM {self._table} WHERE {field_name} = ? ORDER BY id DESC",
            (value,),
        )
        return cur.fetchall()

    def get_latest_row_by_field(self, field_name, value):
        rows = self.get_rows_by_field(field_name, value)
        return rows[0] if rows else None

    def get_option_values(self, table):
        cur = self._db.execute(f"SELECT name FROM {table} ORDER BY name")
        return [row[0] for row in cur.fetchall()]

    def add_option(self, table, name):
        self._db.execute(f"INSERT INTO {table} (name) VALUES (?)", (name,))
        self._db.commit()

    def update_option(self, table, old, new):
        self._db.execute(
            f"UPDATE {table} SET name = ? WHERE name = ?",
            (new, old),
        )
        self._db.commit()

    def delete_option(self, table, name):
        self._db.execute(f"DELETE FROM {table} WHERE name = ?", (name,))
        self._db.commit()

    def close(self):
        self._db.close()

    def get_setting(self, key, default=None):
        cur = self._db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cur.fetchone()
        return row[0] if row and row[0] is not None else default

    def set_setting(self, key, value):
        self._db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._db.commit()

    def add_reported(self, name, serverip, template, category, reported_at, response):
        self._db.execute(
            """
            INSERT INTO reported_domains (name, serverip, template, category, reported_at, response)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (name, serverip, template, category, reported_at, response),
        )
        self._db.commit()

    def list_reported(self):
        cur = self._db.execute(
            "SELECT id, name, serverip, template, category, reported_at, response "
            "FROM reported_domains ORDER BY id DESC"
        )
        return cur.fetchall()

    def delete_reported(self, ids):
        self._db.executemany("DELETE FROM reported_domains WHERE id = ?", [(i,) for i in ids])
        self._db.commit()
