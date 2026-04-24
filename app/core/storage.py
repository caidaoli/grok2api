"""
统一存储服务 (Professional Storage Service)
支持 Local (TOML), Redis, MySQL, PostgreSQL

特性:
- 全异步 I/O (Async I/O)
- 连接池管理 (Connection Pooling)
- 分布式/本地锁 (Distributed/Local Locking)
- 内存优化 (序列化性能优化)
"""

import abc
import os
import asyncio
import hashlib
import time
import tomllib
from typing import Any, Dict, Optional
from pathlib import Path
from enum import Enum
try:
    import fcntl
except ImportError:  # pragma: no cover - non-posix platforms
    fcntl = None
from contextlib import asynccontextmanager

import orjson
import aiofiles
from app.core.logger import logger

# 配置文件路径
CONFIG_FILE = Path(__file__).parent.parent.parent / "data" / "config.toml"
TOKEN_FILE = Path(__file__).parent.parent.parent / "data" / "token.json"
LOCK_DIR = Path(__file__).parent.parent.parent / "data" / ".locks"

# JSON 序列化优化助手函数
def json_dumps(obj: Any) -> str:
    return orjson.dumps(obj).decode("utf-8")

def json_loads(obj: str | bytes) -> Any:
    return orjson.loads(obj)

class StorageError(Exception):
    """存储服务基础异常"""
    pass

class BaseStorage(abc.ABC):
    """存储基类"""

    @abc.abstractmethod
    async def load_config(self) -> Dict[str, Any]:
        """加载配置"""
        pass

    @abc.abstractmethod
    async def save_config(self, data: Dict[str, Any]):
        """保存配置"""
        pass

    @abc.abstractmethod
    async def load_tokens(self) -> Dict[str, Any]:
        """加载所有 Token"""
        pass

    @abc.abstractmethod
    async def save_tokens(self, data: Dict[str, Any]):
        """保存所有 Token"""
        pass

    @staticmethod
    def _normalize_token_key(token: Any) -> str:
        raw = str(token or "").strip()
        if raw.startswith("sso="):
            raw = raw[4:].strip()
        return raw

    async def delete_tokens(self, tokens: list[str]) -> int:
        """删除指定 Token；后端未覆盖时退化为 load-filter-save。"""
        wanted: set[str] = set()
        for token in tokens or []:
            normalized = self._normalize_token_key(token)
            if normalized:
                wanted.add(normalized)
        if not wanted:
            return 0

        data = await self.load_tokens()
        if not isinstance(data, dict):
            return 0

        deleted = 0
        next_data: dict[str, list[Any]] = {}
        for pool_name, items in data.items():
            if not isinstance(items, list):
                next_data[pool_name] = []
                continue

            kept: list[Any] = []
            for item in items:
                token_raw = item if isinstance(item, str) else (item.get("token") if isinstance(item, dict) else "")
                token_key = self._normalize_token_key(token_raw)
                if token_key and token_key in wanted:
                    deleted += 1
                    continue
                kept.append(item)
            next_data[pool_name] = kept

        if deleted:
            await self.save_tokens(next_data)
        return deleted

    @abc.abstractmethod
    async def close(self):
        """关闭资源"""
        pass
    
    @asynccontextmanager
    async def acquire_lock(self, name: str, timeout: int = 10):
        """
        获取锁 (互斥访问)
        用于读写操作的临界区保护
        
        Args:
            name: 锁名称
            timeout: 超时时间 (秒)
        """
        # 默认空实现，用于 fallback
        yield

    async def verify_connection(self) -> bool:
        """健康检查"""
        return True


class LocalStorage(BaseStorage):
    """
    本地文件存储
    - 使用 aiofiles 进行异步 I/O
    - 使用 asyncio.Lock 进行进程内并发控制
    - 如果需要多进程安全，需要系统级文件锁 (fcntl)
    """
    
    def __init__(self):
        self._lock = asyncio.Lock()
        
    @asynccontextmanager
    async def acquire_lock(self, name: str, timeout: int = 10):
        if fcntl is None:
            try:
                async with asyncio.timeout(timeout):
                    async with self._lock:
                        yield
            except asyncio.TimeoutError:
                logger.warning(f"LocalStorage: 获取锁 '{name}' 超时 ({timeout}s)")
                raise StorageError(f"无法获取锁 '{name}'")
            return

        lock_path = LOCK_DIR / f"{name}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = None
        locked = False
        start = time.monotonic()

        try:
            fd = open(lock_path, "a+")
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except BlockingIOError:
                    if time.monotonic() - start >= timeout:
                        raise StorageError(f"无法获取锁 '{name}'")
                    await asyncio.sleep(0.05)
            yield
        except StorageError:
            logger.warning(f"LocalStorage: 获取锁 '{name}' 超时 ({timeout}s)")
            raise
        finally:
            if fd:
                if locked:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except Exception:
                        pass
                try:
                    fd.close()
                except Exception:
                    pass

    async def load_config(self) -> Dict[str, Any]:
        if not CONFIG_FILE.exists():
            return {}
        try:
            async with aiofiles.open(CONFIG_FILE, "rb") as f:
                content = await f.read()
                return tomllib.loads(content.decode("utf-8"))
        except Exception as e:
            logger.error(f"LocalStorage: 加载配置失败: {e}")
            return {}

    async def save_config(self, data: Dict[str, Any]):
        try:
            lines = []
            for section, items in data.items():
                if not isinstance(items, dict): continue
                lines.append(f"[{section}]")
                for key, val in items.items():
                    if isinstance(val, bool):
                        val_str = "true" if val else "false"
                    elif isinstance(val, str):
                        escaped = val.replace('"', '\\"')
                        val_str = f'"{escaped}"'
                    elif isinstance(val, (int, float)):
                        val_str = str(val)
                    elif isinstance(val, (list, dict)):
                        val_str = json_dumps(val)
                    else:
                        val_str = f'"{str(val)}"'
                    lines.append(f"{key} = {val_str}")
                lines.append("")
            
            content = "\n".join(lines)
            
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            async with aiofiles.open(CONFIG_FILE, "w", encoding="utf-8") as f:
                await f.write(content)
        except Exception as e:
            logger.error(f"LocalStorage: 保存配置失败: {e}")
            raise StorageError(f"保存配置失败: {e}")

    async def load_tokens(self) -> Dict[str, Any]:
        if not TOKEN_FILE.exists():
            return {}
        try:
            async with aiofiles.open(TOKEN_FILE, "rb") as f:
                content = await f.read()
                return json_loads(content)
        except Exception as e:
            logger.error(f"LocalStorage: 加载 Token 失败: {e}")
            return {}

    async def save_tokens(self, data: Dict[str, Any]):
        try:
            TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            temp_path = TOKEN_FILE.with_suffix('.tmp')
            
            # 原子写操作: 写入临时文件 -> 重命名
            async with aiofiles.open(temp_path, "wb") as f:
                await f.write(orjson.dumps(data, option=orjson.OPT_INDENT_2))
            
            # 使用 os.replace 保证原子性
            os.replace(temp_path, TOKEN_FILE)
            
        except Exception as e:
            logger.error(f"LocalStorage: 保存 Token 失败: {e}")
            raise StorageError(f"保存 Token 失败: {e}")

    async def close(self):
        pass


class RedisStorage(BaseStorage):
    """
    Redis 存储
    - 使用 redis-py 异步客户端 (自带连接池)
    - 支持分布式锁 (redis.lock)
    - 扁平化数据结构优化性能
    """
    
    def __init__(self, url: str):
        try:
            from redis import asyncio as aioredis
            from redis.asyncio.lock import Lock
        except ImportError:
            raise ImportError("需要安装 redis 包: pip install redis")
            
        # 显式配置连接池
        # 使用 decode_responses=True 简化字符串处理，但在处理复杂对象时使用 orjson
        self.redis = aioredis.from_url(
            url, 
            decode_responses=True,
            health_check_interval=30
        )
        self.config_key = "grok2api:config"       # Hash: section.key -> value_json
        self.key_pools = "grok2api:pools"         # Set: pool_names
        self.prefix_pool_set = "grok2api:pool:"   # Set: pool -> token_ids
        self.prefix_token_hash = "grok2api:token:"# Hash: token_id -> token_data
        self.lock_prefix = "grok2api:lock:"

    @asynccontextmanager
    async def acquire_lock(self, name: str, timeout: int = 10):
        # 使用 Redis 分布式锁
        lock_key = f"{self.lock_prefix}{name}"
        lock = self.redis.lock(lock_key, timeout=timeout, blocking_timeout=5)
        acquired = False
        try:
            acquired = await lock.acquire()
            if not acquired:
                raise StorageError(f"RedisStorage: 无法获取锁 '{name}'")
            yield
        finally:
            if acquired:
                try:
                    await lock.release()
                except Exception:
                    # 锁可能已过期或被意外释放，忽略异常
                    pass

    async def verify_connection(self) -> bool:
        try:
            return await self.redis.ping()
        except Exception:
            return False

    async def load_config(self) -> Dict[str, Any]:
        """从 Redis Hash 加载配置"""
        try:
            raw_data = await self.redis.hgetall(self.config_key)
            if not raw_data:
                return None
                
            config = {}
            for composite_key, val_str in raw_data.items():
                if "." not in composite_key: continue
                section, key = composite_key.split(".", 1)
                
                if section not in config: config[section] = {}
                
                try:
                    val = json_loads(val_str)
                except (ValueError, TypeError):
                    val = val_str
                config[section][key] = val
            return config
        except Exception as e:
            logger.error(f"RedisStorage: 加载配置失败: {e}")
            return None

    async def save_config(self, data: Dict[str, Any]):
        """保存配置到 Redis Hash"""
        if not data: return
        try:
            mapping = {}
            for section, items in data.items():
                if not isinstance(items, dict): continue
                for key, val in items.items():
                    composite_key = f"{section}.{key}"
                    mapping[composite_key] = json_dumps(val)
            
            if mapping:
                await self.redis.hset(self.config_key, mapping=mapping)
        except Exception as e:
            logger.error(f"RedisStorage: 保存配置失败: {e}")
            raise

    async def load_tokens(self) -> Dict[str, Any]:
        """加载所有 Token"""
        try:
            pool_names = await self.redis.smembers(self.key_pools)
            if not pool_names: return {}
            
            pools = {}
            async with self.redis.pipeline() as pipe:
                for pool_name in pool_names:
                    # 获取该池下所有 Token ID
                    pipe.smembers(f"{self.prefix_pool_set}{pool_name}")
                pool_tokens_res = await pipe.execute()
            
            # 收集所有 Token ID 以便批量查询
            all_token_ids = []
            pool_map = {} # pool_name -> list[token_id]
            
            for i, pool_name in enumerate(pool_names):
                tids = list(pool_tokens_res[i])
                pool_map[pool_name] = tids
                all_token_ids.extend(tids)
                
            if not all_token_ids:
                return {name: [] for name in pool_names}

            # 批量获取 Token 详情 (Hash)
            async with self.redis.pipeline() as pipe:
                for tid in all_token_ids:
                    pipe.hgetall(f"{self.prefix_token_hash}{tid}")
                token_data_list = await pipe.execute()
                
            # 重组数据结构
            token_lookup = {}
            for i, tid in enumerate(all_token_ids):
                t_data = token_data_list[i]
                if not t_data: continue
                
                # 恢复 tags (JSON -> List)
                if "tags" in t_data:
                    try: t_data["tags"] = json_loads(t_data["tags"])
                    except (ValueError, TypeError): t_data["tags"] = []
                
                # 类型转换 (Redis 返回全 string)
                for int_field in ["quota", "created_at", "use_count", "fail_count", "last_used_at", "last_fail_at", "last_sync_at"]:
                    if t_data.get(int_field) and t_data[int_field] != "None":
                         try: t_data[int_field] = int(t_data[int_field])
                         except (ValueError, TypeError): pass
                         
                token_lookup[tid] = t_data

            # 按 Pool 分组返回
            for pool_name in pool_names:
                pools[pool_name] = []
                for tid in pool_map[pool_name]:
                    if tid in token_lookup:
                        pools[pool_name].append(token_lookup[tid])
                        
            return pools
            
        except Exception as e:
            logger.error(f"RedisStorage: 加载 Token 失败: {e}")
            return None

    async def save_tokens(self, data: Dict[str, Any]):
        """保存所有 Token"""
        if data is None:
            return
        try:
            new_pools = set(data.keys()) if isinstance(data, dict) else set()
            pool_tokens_map = {}
            new_token_ids = set()

            for pool_name, tokens in (data or {}).items():
                tids_in_pool = []
                for t in tokens:
                    token_str = t.get("token")
                    if not token_str:
                        continue
                    tids_in_pool.append(token_str)
                    new_token_ids.add(token_str)
                pool_tokens_map[pool_name] = tids_in_pool

            existing_pools = await self.redis.smembers(self.key_pools)
            existing_pools = set(existing_pools) if existing_pools else set()

            existing_token_ids = set()
            if existing_pools:
                async with self.redis.pipeline() as pipe:
                    for pool_name in existing_pools:
                        pipe.smembers(f"{self.prefix_pool_set}{pool_name}")
                    pool_tokens_res = await pipe.execute()
                for tokens in pool_tokens_res:
                    existing_token_ids.update(list(tokens or []))

            tokens_to_delete = existing_token_ids - new_token_ids
            all_pools = existing_pools.union(new_pools)

            async with self.redis.pipeline() as pipe:
                # Reset pool index
                pipe.delete(self.key_pools)
                if new_pools:
                    pipe.sadd(self.key_pools, *new_pools)

                # Reset pool sets
                for pool_name in all_pools:
                    pipe.delete(f"{self.prefix_pool_set}{pool_name}")
                for pool_name, tids_in_pool in pool_tokens_map.items():
                    if tids_in_pool:
                        pipe.sadd(f"{self.prefix_pool_set}{pool_name}", *tids_in_pool)

                # Remove deleted token hashes
                for token_str in tokens_to_delete:
                    pipe.delete(f"{self.prefix_token_hash}{token_str}")

                # Upsert token hashes
                for pool_name, tokens in (data or {}).items():
                    for t in tokens:
                        token_str = t.get("token")
                        if not token_str:
                            continue
                        t_flat = t.copy()
                        if "tags" in t_flat:
                            t_flat["tags"] = json_dumps(t_flat["tags"])
                        status = t_flat.get("status")
                        if isinstance(status, str) and status.startswith("TokenStatus."):
                            t_flat["status"] = status.split(".", 1)[1].lower()
                        elif isinstance(status, Enum):
                            t_flat["status"] = status.value
                        t_flat = {k: str(v) for k, v in t_flat.items() if v is not None}
                        pipe.hset(f"{self.prefix_token_hash}{token_str}", mapping=t_flat)

                await pipe.execute()
                
        except Exception as e:
            logger.error(f"RedisStorage: 保存 Token 失败: {e}")
            raise

    async def delete_tokens(self, tokens: list[str]) -> int:
        """定向删除 Token，避免 Redis 后端全量重写。"""
        token_ids = list(dict.fromkeys(
            token for token in (self._normalize_token_key(t) for t in (tokens or [])) if token
        ))
        if not token_ids:
            return 0

        try:
            pool_names = await self.redis.smembers(self.key_pools)
            pool_names = list(pool_names or [])

            async with self.redis.pipeline() as pipe:
                for token in token_ids:
                    pipe.delete(f"{self.prefix_token_hash}{token}")
                for pool_name in pool_names:
                    pipe.srem(f"{self.prefix_pool_set}{pool_name}", *token_ids)
                results = await pipe.execute()

            deleted_hashes = results[:len(token_ids)]
            return sum(int(v or 0) for v in deleted_hashes)
        except Exception as e:
            logger.error(f"RedisStorage: 删除 Token 失败: {e}")
            raise

    async def close(self):
        try:
            await self.redis.close()
        except (RuntimeError, asyncio.CancelledError, Exception):
             # 忽略关闭时的 Event loop is closed 错误
            pass


class SQLStorage(BaseStorage):
    """
    SQL 数据库存储 (MySQL/PgSQL)
    - 使用 SQLAlchemy 异步引擎
    - 自动 Schema 初始化
    - 内置连接池 (QueuePool)
    """

    def __init__(self, url: str):
        try:
            from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
            from sqlalchemy import text, MetaData
        except ImportError:
            raise ImportError("需要安装 sqlalchemy 和 async 驱动: pip install sqlalchemy[asyncio]")

        self.dialect = url.split(":", 1)[0].split("+", 1)[0].lower()

        # Read pool params directly from TOML files to avoid circular dependency
        # (config.load() depends on storage, which we're creating right now)
        db_cfg = self._read_db_config()

        self.engine = create_async_engine(
            url,
            echo=False,
            pool_size=db_cfg["pool_size"],
            max_overflow=db_cfg["max_overflow"],
            pool_recycle=db_cfg["pool_recycle"],
            pool_pre_ping=db_cfg["pool_pre_ping"],
        )
        self.async_session = async_sessionmaker(self.engine, expire_on_commit=False)
        self._initialized = False
    
    @staticmethod
    def _read_db_config() -> dict:
        """Read [database] section directly from TOML files (no storage dependency)."""
        defaults = {"pool_size": 20, "max_overflow": 10, "pool_recycle": 3600, "pool_pre_ping": True}
        defaults_file = Path(__file__).parent.parent.parent / "config.defaults.toml"
        override_file = CONFIG_FILE  # data/config.toml
        for path in (defaults_file, override_file):
            if not path.exists():
                continue
            try:
                with path.open("rb") as f:
                    data = tomllib.load(f)
                db = data.get("database")
                if isinstance(db, dict):
                    for k in defaults:
                        if k in db:
                            defaults[k] = type(defaults[k])(db[k])
            except Exception:
                pass
        return defaults

    async def _ensure_schema(self):
        """确保数据库表存在"""
        if self._initialized: return
        from sqlalchemy import text

        # Fast path: single query probes both tables in one roundtrip
        try:
            async with self.engine.connect() as conn:
                await conn.execute(text(
                    "SELECT 1 FROM tokens WHERE 1=0 "
                    "UNION ALL "
                    "SELECT 1 FROM app_config WHERE 1=0"
                ))
            self._initialized = True
            return
        except Exception:
            pass

        # Slow path: full DDL — only runs on first-ever startup
        try:
            async with self.engine.begin() as conn:
                token_pk_type = "VARCHAR(191)" if self.dialect in ("mysql", "mariadb") else "VARCHAR(512)"

                await conn.execute(text(f"""
                    CREATE TABLE IF NOT EXISTS tokens (
                        token {token_pk_type} PRIMARY KEY,
                        pool_name VARCHAR(64) NOT NULL,
                        data TEXT,
                        updated_at BIGINT
                    )
                """))

                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS app_config (
                        section VARCHAR(64) NOT NULL,
                        key_name VARCHAR(64) NOT NULL,
                        value TEXT,
                        PRIMARY KEY (section, key_name)
                    )
                """))

                try:
                    await conn.execute(text("CREATE INDEX idx_tokens_pool ON tokens (pool_name)"))
                except Exception:
                    pass

                try:
                    if self.dialect in ("mysql", "mariadb"):
                        await conn.execute(text("ALTER TABLE tokens MODIFY data TEXT"))
                    elif self.dialect in ("postgres", "postgresql", "pgsql"):
                        await conn.execute(text("ALTER TABLE tokens ALTER COLUMN token TYPE VARCHAR(512)"))
                        await conn.execute(text("ALTER TABLE tokens ALTER COLUMN data TYPE TEXT"))
                except Exception:
                    pass

            self._initialized = True
        except Exception as e:
            logger.error(f"SQLStorage: Schema 初始化失败: {e}")
            raise

    @asynccontextmanager
    async def acquire_lock(self, name: str, timeout: int = 10):
        # SQL 分布式锁: MySQL GET_LOCK / PG advisory_lock
        from sqlalchemy import text
        lock_name = f"g2a:{hashlib.sha1(name.encode('utf-8')).hexdigest()[:24]}"
        if self.dialect in ("mysql", "mariadb"):
            async with self.async_session() as session:
                res = await session.execute(
                    text("SELECT GET_LOCK(:name, :timeout)"),
                    {"name": lock_name, "timeout": timeout}
                )
                got = res.scalar()
                if got != 1:
                    raise StorageError(f"SQLStorage: 无法获取锁 '{name}'")
                try:
                    yield
                finally:
                    try:
                        await session.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": lock_name})
                        await session.commit()
                    except Exception:
                        pass
        elif self.dialect in ("postgres", "postgresql", "pgsql"):
            lock_key = int.from_bytes(hashlib.sha256(name.encode("utf-8")).digest()[:8], "big", signed=False)
            async with self.async_session() as session:
                start = time.monotonic()
                while True:
                    res = await session.execute(
                        text("SELECT pg_try_advisory_lock(:key)"),
                        {"key": lock_key}
                    )
                    if res.scalar():
                        break
                    if time.monotonic() - start >= timeout:
                        raise StorageError(f"SQLStorage: 无法获取锁 '{name}'")
                    await asyncio.sleep(0.1)
                try:
                    yield
                finally:
                    try:
                        await session.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key})
                        await session.commit()
                    except Exception:
                        pass
        else:
            yield

    async def load_config(self) -> Dict[str, Any]:
        await self._ensure_schema()
        from sqlalchemy import text
        try:
            async with self.async_session() as session:
                res = await session.execute(text("SELECT section, key_name, value FROM app_config"))
                rows = res.fetchall()
                if not rows: return None
                
                config = {}
                for section, key, val_str in rows:
                    if section not in config: config[section] = {}
                    try:
                        val = json_loads(val_str)
                    except (ValueError, TypeError):
                        val = val_str
                    config[section][key] = val
                return config
        except Exception as e:
            logger.error(f"SQLStorage: 加载配置失败: {e}")
            return None

    async def save_config(self, data: Dict[str, Any]):
        await self._ensure_schema()
        from sqlalchemy import text
        try:
            params = []
            for section, items in data.items():
                if not isinstance(items, dict):
                    continue
                for key, val in items.items():
                    params.append({"s": section, "k": key, "v": json_dumps(val)})
            if not params:
                return

            # Build single multi-row INSERT to avoid N roundtrips from executemany
            flat = {}
            placeholders = []
            for i, p in enumerate(params):
                flat[f"s{i}"] = p["s"]
                flat[f"k{i}"] = p["k"]
                flat[f"v{i}"] = p["v"]
                placeholders.append(f"(:s{i}, :k{i}, :v{i})")
            values_clause = ", ".join(placeholders)

            async with self.async_session() as session:
                if self.dialect in ("mysql", "mariadb"):
                    stmt = text(
                        f"INSERT INTO app_config (section, key_name, value) VALUES {values_clause} "
                        "ON DUPLICATE KEY UPDATE value=VALUES(value)"
                    )
                else:
                    stmt = text(
                        f"INSERT INTO app_config (section, key_name, value) VALUES {values_clause} "
                        "ON CONFLICT (section, key_name) DO UPDATE SET value=EXCLUDED.value"
                    )
                await session.execute(stmt, flat)

                # Clean up stale sections no longer in current config
                sections = {p["s"] for p in params}
                sec_params = {f"sec{i}": s for i, s in enumerate(sections)}
                sec_placeholders = ", ".join(f":{n}" for n in sec_params)
                await session.execute(
                    text(f"DELETE FROM app_config WHERE section NOT IN ({sec_placeholders})"),
                    sec_params,
                )

                await session.commit()
        except Exception as e:
            logger.error(f"SQLStorage: 保存配置失败: {e}")
            raise

    async def load_tokens(self) -> Dict[str, Any]:
        await self._ensure_schema()
        from sqlalchemy import text
        try:
            async with self.async_session() as session:
                res = await session.execute(text("SELECT pool_name, data FROM tokens"))
                rows = res.fetchall()
                if not rows: return {}
                
                pools = {}
                for pool_name, data_json in rows:
                    if pool_name not in pools: pools[pool_name] = []
                    
                    try:
                        if isinstance(data_json, str):
                            t_data = json_loads(data_json)
                        else:
                            t_data = data_json
                        pools[pool_name].append(t_data)
                    except (ValueError, TypeError):
                        pass
                return pools
        except Exception as e:
            logger.error(f"SQLStorage: 加载 Token 失败: {e}")
            return None

    async def save_tokens(self, data: Dict[str, Any]):
        await self._ensure_schema()
        from sqlalchemy import text
        try:
            async with self.async_session() as session:
                all_token_keys = set()
                params = []
                for pool_name, tokens in data.items():
                    for t in tokens:
                        token_key = t.get("token")
                        if not token_key:
                            continue
                        all_token_keys.add(token_key)
                        params.append({
                            "token": token_key,
                            "pool_name": pool_name,
                            "data": json_dumps(t),
                            "updated_at": 0
                        })

                # Batch UPSERT — single executemany call instead of N round-trips
                if params:
                    if self.dialect in ("mysql", "mariadb"):
                        await session.execute(
                            text("INSERT INTO tokens (token, pool_name, data, updated_at) "
                                 "VALUES (:token, :pool_name, :data, :updated_at) "
                                 "ON DUPLICATE KEY UPDATE pool_name=VALUES(pool_name), "
                                 "data=VALUES(data), updated_at=VALUES(updated_at)"),
                            params
                        )
                    else:
                        await session.execute(
                            text("INSERT INTO tokens (token, pool_name, data, updated_at) "
                                 "VALUES (:token, :pool_name, :data, :updated_at) "
                                 "ON CONFLICT (token) DO UPDATE SET pool_name=EXCLUDED.pool_name, "
                                 "data=EXCLUDED.data, updated_at=EXCLUDED.updated_at"),
                            params
                        )

                # Single-statement stale cleanup instead of SELECT + per-row DELETE
                if all_token_keys:
                    # Bind each key as a named param for safe IN clause
                    key_params = {f"k{i}": k for i, k in enumerate(all_token_keys)}
                    placeholders = ", ".join(f":{name}" for name in key_params)
                    await session.execute(
                        text(f"DELETE FROM tokens WHERE token NOT IN ({placeholders})"),
                        key_params,
                    )
                else:
                    # No tokens at all — wipe the table
                    await session.execute(text("DELETE FROM tokens"))

                await session.commit()
        except Exception as e:
            logger.error(f"SQLStorage: 保存 Token 失败: {e}")
            raise

    async def delete_tokens(self, tokens: list[str]) -> int:
        """定向删除 Token，避免 MySQL/PgSQL 保存全量 token 集。"""
        await self._ensure_schema()
        from sqlalchemy import text

        token_keys = list(dict.fromkeys(
            token for token in (self._normalize_token_key(t) for t in (tokens or [])) if token
        ))
        if not token_keys:
            return 0

        deleted = 0
        chunk_size = 500
        try:
            async with self.async_session() as session:
                for start in range(0, len(token_keys), chunk_size):
                    chunk = token_keys[start:start + chunk_size]
                    params = {f"t{i}": token for i, token in enumerate(chunk)}
                    placeholders = ", ".join(f":{name}" for name in params)
                    result = await session.execute(
                        text(f"DELETE FROM tokens WHERE token IN ({placeholders})"),
                        params,
                    )
                    rowcount = getattr(result, "rowcount", 0)
                    if isinstance(rowcount, int) and rowcount > 0:
                        deleted += rowcount
                await session.commit()
            return deleted
        except Exception as e:
            logger.error(f"SQLStorage: 删除 Token 失败: {e}")
            raise

    async def close(self):
        await self.engine.dispose()


class StorageFactory:
    """存储后端工厂"""
    _instance: Optional[BaseStorage] = None
    
    @classmethod
    def get_storage(cls) -> BaseStorage:
        """获取全局存储实例 (单例)"""
        if cls._instance:
            return cls._instance
            
        storage_type = os.getenv("SERVER_STORAGE_TYPE", "local").lower()
        storage_url = os.getenv("SERVER_STORAGE_URL", "")
        
        logger.info(f"StorageFactory: 初始化存储后端: {storage_type}")
        
        if storage_type == "redis":
            if not storage_url: raise ValueError("Redis 存储需要设置 SERVER_STORAGE_URL")
            cls._instance = RedisStorage(storage_url)
            
        elif storage_type in ("mysql", "pgsql"):
            if not storage_url: raise ValueError("SQL 存储需要设置 SERVER_STORAGE_URL")
            cls._instance = SQLStorage(storage_url)
            
        else:
            cls._instance = LocalStorage()
            
        return cls._instance

def get_storage() -> BaseStorage:
    return StorageFactory.get_storage()
