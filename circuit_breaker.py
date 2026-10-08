import time
import asyncio
import logging

class RequestCircuitBreaker:
    """Discord APIへの過剰通信（連打・ループ）を瞬時に検知し、API送信前に事前遮断するクラス"""
    def __init__(self, max_requests: int = 10, window_seconds: float = 5.0, cooldown_seconds: float = 60.0):
        self.max_requests = max_requests  # 5秒間に最大10リクエストまで許可 (より安全なマージン)
        self.window_seconds = window_seconds
        self.cooldown_seconds = cooldown_seconds
        self._timestamps = []
        self._lock = asyncio.Lock()
        self.is_tripped = False
        self.tripped_until = 0.0
        self.maintenance_mode = False

    async def can_proceed(self) -> bool:
        """リクエスト・操作実行前に呼び出し、通信を許可するか事前遮断するか判定する"""
        if self.maintenance_mode:
            return False

        now = time.time()
        async with self._lock:
            # 遮断中かチェック
            if self.is_tripped:
                if now < self.tripped_until:
                    return False
                else:
                    self.is_tripped = False
                    self._timestamps.clear()
                    logging.getLogger("discord").info("🛡️ [Circuit Breaker] クールダウン完了: 通常稼働に自動復帰しました。")

            # 古いタイムスタンプをスライディングウィンドウから除外
            cutoff = now - self.window_seconds
            self._timestamps = [t for t in self._timestamps if t > cutoff]

            # 閾値超過判定 (短時間の過剰リクエストを事前検知)
            if len(self._timestamps) >= self.max_requests:
                self.is_tripped = True
                self.tripped_until = now + self.cooldown_seconds
                logging.getLogger("discord").warning(
                    f"🚨 [Circuit Breaker 発動] 短時間に過剰なリクエスト ({len(self._timestamps)}回 / {self.window_seconds}秒) を検知！\n"
                    f"   Cloudflare/DiscordによるIPブロックを防ぐため、{self.cooldown_seconds}秒間リクエストを事前遮断します。"
                )
                return False

            self._timestamps.append(now)
            return True

    def trip_manually(self, reason: str = "手動"):
        self.is_tripped = True
        self.tripped_until = time.time() + self.cooldown_seconds
        logging.getLogger("discord").warning(f"🚨 [Circuit Breaker 手動発動] 理由: {reason} ({self.cooldown_seconds}秒遮断)")

    def reset(self):
        self.is_tripped = False
        self._timestamps.clear()
        self.tripped_until = 0.0
        logging.getLogger("discord").info("🛡️ [Circuit Breaker] 手動でリセットされました。")


# グローバル共有インスタンス
circuit_breaker = RequestCircuitBreaker()
