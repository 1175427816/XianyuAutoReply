import sqlite3
import os
import json
from datetime import datetime, timedelta
from loguru import logger


class ChatContextManager:
    """
    聊天上下文管理器
    
    负责存储和检索用户与商品之间的对话历史，使用SQLite数据库进行持久化存储。
    支持按会话ID检索对话历史，以及议价次数统计。
    """
    
    def __init__(self, max_history=100, db_path="data/chat_history.db"):
        """
        初始化聊天上下文管理器
        
        Args:
            max_history: 每个对话保留的最大消息数
            db_path: SQLite数据库文件路径
        """
        self.max_history = max_history
        self.db_path = db_path
        self._init_db()
        
    def _init_db(self):
        """初始化数据库表结构"""
        # 确保数据库目录存在
        db_dir = os.path.dirname(self.db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir)
            
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # 创建消息表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            item_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            chat_id TEXT
        )
        ''')
        
        # 检查是否需要添加chat_id字段（兼容旧数据库）
        cursor.execute("PRAGMA table_info(messages)")
        columns = [column[1] for column in cursor.fetchall()]
        if 'chat_id' not in columns:
            cursor.execute('ALTER TABLE messages ADD COLUMN chat_id TEXT')
            logger.info("已为messages表添加chat_id字段")
        
        # 创建索引以加速查询
        cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_user_item ON messages (user_id, item_id)
        ''')
        
        cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_chat_id ON messages (chat_id)
        ''')
        
        cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_timestamp ON messages (timestamp)
        ''')

        # 已处理消息表，用于避免 WebSocket 重连/同步补偿造成重复回复
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS processed_messages (
            message_key TEXT PRIMARY KEY,
            chat_id TEXT,
            item_id TEXT,
            processed_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        ''')

        cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_processed_at ON processed_messages (processed_at)
        ''')

        # 用户显式恢复自动回复的会话。用于覆盖“卖家先发起会话默认人工接管”的保护规则。
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS chat_auto_reply_overrides (
            chat_id TEXT PRIMARY KEY,
            enabled INTEGER DEFAULT 1,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        
        # 创建基于会话ID的议价次数表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS chat_bargain_counts (
            chat_id TEXT PRIMARY KEY,
            count INTEGER DEFAULT 0,
            last_updated DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        
        # 创建商品信息表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS items (
            item_id TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            price REAL,
            description TEXT,
            last_updated DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        
        conn.commit()
        conn.close()
        logger.info(f"聊天历史数据库初始化完成: {self.db_path}")
        

            
    def save_item_info(self, item_id, item_data):
        """
        保存商品信息到数据库
        
        Args:
            item_id: 商品ID
            item_data: 商品信息字典
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            # 从商品数据中提取有用信息
            price = float(item_data.get('soldPrice', 0))
            description = item_data.get('desc', '')
            
            # 将整个商品数据转换为JSON字符串
            data_json = json.dumps(item_data, ensure_ascii=False)
            
            cursor.execute(
                """
                INSERT INTO items (item_id, data, price, description, last_updated) 
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(item_id) 
                DO UPDATE SET data = ?, price = ?, description = ?, last_updated = ?
                """,
                (
                    item_id, data_json, price, description, datetime.now().isoformat(),
                    data_json, price, description, datetime.now().isoformat()
                )
            )
            
            conn.commit()
            logger.debug(f"商品信息已保存: {item_id}")
        except Exception as e:
            logger.error(f"保存商品信息时出错: {e}")
            conn.rollback()
        finally:
            conn.close()
    
    def get_item_info(self, item_id, max_age_seconds=None):
        """
        从数据库获取商品信息
        
        Args:
            item_id: 商品ID
            max_age_seconds: 缓存最大有效秒数，None表示不检查过期
            
        Returns:
            dict: 商品信息字典，如果不存在返回None
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute(
                "SELECT data, last_updated FROM items WHERE item_id = ?",
                (item_id,)
            )
            
            result = cursor.fetchone()
            if result:
                if max_age_seconds is not None and max_age_seconds > 0:
                    try:
                        last_updated = datetime.fromisoformat(result[1])
                        if datetime.now() - last_updated > timedelta(seconds=max_age_seconds):
                            logger.info(f"商品信息缓存已过期: {item_id}")
                            return None
                    except Exception as e:
                        logger.warning(f"商品缓存时间解析失败，将重新获取: {item_id}, {e}")
                        return None
                return json.loads(result[0])
            return None
        except Exception as e:
            logger.error(f"获取商品信息时出错: {e}")
            return None
        finally:
            conn.close()

    def add_message_by_chat(self, chat_id, user_id, item_id, role, content):
        """
        基于会话ID添加新消息到对话历史
        
        Args:
            chat_id: 会话ID
            user_id: 用户ID (用户消息存真实user_id，助手消息存卖家ID)
            item_id: 商品ID
            role: 消息角色 (user/assistant)
            content: 消息内容
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            # 插入新消息，使用chat_id作为额外标识
            cursor.execute(
                "INSERT INTO messages (user_id, item_id, role, content, timestamp, chat_id) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, item_id, role, content, datetime.now().isoformat(), chat_id)
            )
            
            # 检查是否需要清理旧消息（基于chat_id）
            cursor.execute(
                """
                SELECT id FROM messages 
                WHERE chat_id = ? 
                ORDER BY timestamp DESC 
                LIMIT ?, 1
                """, 
                (chat_id, self.max_history)
            )
            
            oldest_to_keep = cursor.fetchone()
            if oldest_to_keep:
                cursor.execute(
                    "DELETE FROM messages WHERE chat_id = ? AND id < ?",
                    (chat_id, oldest_to_keep[0])
                )
            
            conn.commit()
        except Exception as e:
            logger.error(f"添加消息到数据库时出错: {e}")
            conn.rollback()
        finally:
            conn.close()

    def get_context_by_chat(self, chat_id):
        """
        基于会话ID获取对话历史
        
        Args:
            chat_id: 会话ID
            
        Returns:
            list: 包含对话历史的列表
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute(
                """
                SELECT role, content FROM messages 
                WHERE chat_id = ? 
                ORDER BY timestamp ASC
                LIMIT ?
                """, 
                (chat_id, self.max_history)
            )
            
            messages = [{"role": role, "content": content} for role, content in cursor.fetchall()]
            
            # 获取议价次数并添加到上下文中
            bargain_count = self.get_bargain_count_by_chat(chat_id)
            if bargain_count > 0:
                messages.append({
                    "role": "system", 
                    "content": f"议价次数: {bargain_count}"
                })
            
        except Exception as e:
            logger.error(f"获取对话历史时出错: {e}")
            messages = []
        finally:
            conn.close()
        
        return messages

    def is_chat_started_by_assistant(self, chat_id):
        """
        判断会话是否由卖家主动发起。

        如果该会话记录中的第一条消息是assistant，说明是卖家先发起或先人工回复，
        后续不应自动接管回复，避免打断人工沟通。
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            cursor.execute(
                """
                SELECT role FROM messages
                WHERE chat_id = ?
                ORDER BY timestamp ASC, id ASC
                LIMIT 1
                """,
                (chat_id,)
            )
            result = cursor.fetchone()
            return bool(result and result[0] == "assistant")
        except Exception as e:
            logger.error(f"判断会话发起方时出错: {e}")
            return False
        finally:
            conn.close()

    def set_auto_reply_override(self, chat_id, enabled):
        """记录用户对某个会话的显式自动回复选择。"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            if enabled:
                cursor.execute(
                    """
                    INSERT INTO chat_auto_reply_overrides (chat_id, enabled, updated_at)
                    VALUES (?, 1, ?)
                    ON CONFLICT(chat_id)
                    DO UPDATE SET enabled = 1, updated_at = ?
                    """,
                    (chat_id, datetime.now().isoformat(), datetime.now().isoformat())
                )
            else:
                cursor.execute(
                    "DELETE FROM chat_auto_reply_overrides WHERE chat_id = ?",
                    (chat_id,)
                )
            conn.commit()
        except Exception as e:
            logger.error(f"更新自动回复覆盖状态时出错: {e}")
            conn.rollback()
        finally:
            conn.close()

    def has_auto_reply_override(self, chat_id):
        """判断会话是否被用户显式恢复为自动回复。"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            cursor.execute(
                """
                SELECT 1 FROM chat_auto_reply_overrides
                WHERE chat_id = ? AND enabled = 1
                LIMIT 1
                """,
                (chat_id,)
            )
            return cursor.fetchone() is not None
        except Exception as e:
            logger.error(f"读取自动回复覆盖状态时出错: {e}")
            return False
        finally:
            conn.close()

    def increment_bargain_count_by_chat(self, chat_id):
        """
        基于会话ID增加议价次数
        
        Args:
            chat_id: 会话ID
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            # 使用UPSERT语法直接基于chat_id增加议价次数
            cursor.execute(
                """
                INSERT INTO chat_bargain_counts (chat_id, count, last_updated)
                VALUES (?, 1, ?)
                ON CONFLICT(chat_id) 
                DO UPDATE SET count = count + 1, last_updated = ?
                """,
                (chat_id, datetime.now().isoformat(), datetime.now().isoformat())
            )
            
            conn.commit()
            logger.debug(f"会话 {chat_id} 议价次数已增加")
        except Exception as e:
            logger.error(f"增加议价次数时出错: {e}")
            conn.rollback()
        finally:
            conn.close()

    def get_bargain_count_by_chat(self, chat_id):
        """
        基于会话ID获取议价次数
        
        Args:
            chat_id: 会话ID
            
        Returns:
            int: 议价次数
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute(
                "SELECT count FROM chat_bargain_counts WHERE chat_id = ?",
                (chat_id,)
            )
            
            result = cursor.fetchone()
            return result[0] if result else 0
        except Exception as e:
            logger.error(f"获取议价次数时出错: {e}")
            return 0
        finally:
            conn.close() 

    def is_message_processed(self, message_key):
        """
        判断消息是否已经处理过。

        Args:
            message_key: 由会话、发送方、时间和内容生成的稳定消息键
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            cursor.execute(
                "SELECT 1 FROM processed_messages WHERE message_key = ? LIMIT 1",
                (message_key,)
            )
            return cursor.fetchone() is not None
        except Exception as e:
            logger.error(f"检查消息幂等状态时出错: {e}")
            return False
        finally:
            conn.close()

    def mark_message_processed(self, message_key, chat_id=None, item_id=None):
        """
        标记消息已处理。使用 INSERT OR IGNORE 保持幂等。
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            cursor.execute(
                """
                INSERT OR IGNORE INTO processed_messages (message_key, chat_id, item_id, processed_at)
                VALUES (?, ?, ?, ?)
                """,
                (message_key, chat_id, item_id, datetime.now().isoformat())
            )
            conn.commit()
        except Exception as e:
            logger.error(f"标记消息已处理时出错: {e}")
            conn.rollback()
        finally:
            conn.close()

    def cleanup_processed_messages(self, ttl_seconds):
        """
        清理过旧的已处理消息记录，避免幂等表无限增长。
        """
        if ttl_seconds <= 0:
            return

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            cutoff = (datetime.now() - timedelta(seconds=ttl_seconds)).isoformat()
            cursor.execute(
                "DELETE FROM processed_messages WHERE processed_at < ?",
                (cutoff,)
            )
            deleted = cursor.rowcount
            conn.commit()
            if deleted:
                logger.debug(f"已清理过期消息幂等记录: {deleted} 条")
        except Exception as e:
            logger.error(f"清理消息幂等记录时出错: {e}")
            conn.rollback()
        finally:
            conn.close()
