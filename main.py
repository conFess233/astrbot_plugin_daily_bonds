"""今日姻缘插件入口。"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType, event_message_type
from astrbot.api.message_components import Image, Plain

from .services.host_config import HostConfigBridge
from .services.catalog import CatalogService
from .services.storage import SQLiteStorage
from .services.access import AccessService
from .services.gameplay import GameplayService
from .services.statistics import StatisticsService
from .adapters.onebot import OneBotAdapter
from .services.command_runtime import CommandRuntime, RuntimeReply
from .services.notifications import NotificationService
from .services.web_manager import WebManager
from .services.retention import RetentionService


class DailyBondsPlugin(Star):
    """初始化插件服务并处理 OneBot 群消息。"""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        # Star does not assign the plugin-specific _conf_schema.json values.
        # AstrBot injects them as the second constructor argument.
        self.config = config
        self._storage: SQLiteStorage | None = None
        self._catalog_task: asyncio.Task[None] | None = None
        self._runtime: CommandRuntime | None = None
        self._maintenance_task: asyncio.Task[None] | None = None
        self._web_manager: WebManager | None = None

    async def initialize(self) -> None:
        """建立插件数据目录、验证数据库並确保全局默认配置。"""

        if not self.config.get("enabled", True):
            logger.info("今日姻缘：宿主总开关关闭，跳过初始化。")
            return
        data_dir = Path(StarTools.get_data_dir("astrbot_plugin_daily_bonds"))
        storage = SQLiteStorage(data_dir / "state.sqlite3")
        try:
            await asyncio.to_thread(storage.open)
            host_config = HostConfigBridge(self.config, storage)
            settings = host_config.reconcile()
            catalog = CatalogService(
                storage,
                Path(__file__).parent / "resources",
                data_dir / "media",
            )
            await asyncio.to_thread(catalog.seed_builtin_catalog, int(time.time()))
            self._storage = storage
            web_manager = WebManager(self.context, storage, host_config)
            web_manager.register()
            self._web_manager = web_manager
            notifications = NotificationService(self.context, storage)
            retention = RetentionService(storage, data_dir)
            stale_invites = await notifications.invalidate_pending_on_restart(int(time.time()))
            if stale_invites:
                logger.info("今日姻缘：重载时将 %d 条待处理赠送邀请标记为失效。", stale_invites)
            self._runtime = CommandRuntime(
                context=self.context,
                storage=storage,
                adapter=OneBotAdapter(self.context, storage),
                access=AccessService(),
                gameplay=GameplayService(storage),
                statistics=StatisticsService(storage),
            )
            web_manager.runtime_reset = self._runtime.adapter.invalidate_all_members
            self._maintenance_task = asyncio.create_task(
                self._maintenance_loop(notifications, retention),
                name="daily-bonds-maintenance",
            )
            resources = settings["resources"]
            self._catalog_task = asyncio.create_task(
                self._download_catalog(catalog, resources),
                name="daily-bonds-catalog-download",
            )
            logger.info("今日姻缘：数据存储和内置角色目录已就绪。")
        except Exception:
            await asyncio.to_thread(storage.close)
            logger.exception("今日姻缘：插件数据初始化失败。")
            raise

    @filter.event_message_type(EventMessageType.ALL)
    async def on_group_message(self, event: AstrMessageEvent):
        """只接管原始载荷可验证的 OneBot 群消息；普通聊天继续传递。"""

        runtime = self._runtime
        if runtime is None:
            return
        try:
            response = await runtime.handle_group_message(event)
        except Exception:
            logger.exception("今日姻缘：处理群消息失败。")
            return
        if isinstance(response, RuntimeReply):
            if not response.image_paths:
                yield event.plain_result(response.text)
                return
            try:
                chain = ([Plain(response.text)] if response.text else []) + [
                    Image.fromFileSystem(str(image_path)) for image_path in response.image_paths
                ]
                yield event.chain_result(chain)
            finally:
                for image_path in response.cleanup_paths:
                    image_path.unlink(missing_ok=True)
        elif response:
            yield event.plain_result(response)

    async def terminate(self) -> None:
        """停止种子素材下载并关闭数据库连接。"""

        task, self._catalog_task = self._catalog_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        maintenance, self._maintenance_task = self._maintenance_task, None
        if maintenance is not None:
            maintenance.cancel()
            await asyncio.gather(maintenance, return_exceptions=True)
        storage, self._storage = self._storage, None
        if storage is not None:
            await asyncio.to_thread(storage.close)

    async def _download_catalog(self, catalog: CatalogService, resources: dict) -> None:
        try:
            result = await catalog.download_builtin_images(
                timeout_seconds=resources["http_timeout_seconds"],
                max_bytes=resources["image_max_bytes"],
                max_pixels=resources["image_max_pixels"],
                concurrency=resources["download_concurrency"],
            )
            if result.unavailable:
                logger.warning(
                    "今日姻缘：内置素材下载完成，成功=%d，已有=%d，失败=%d。",
                    result.downloaded,
                    result.already_present,
                    len(result.unavailable),
                )
            else:
                logger.info(
                    "今日姻缘：内置素材就绪，下载=%d，已有=%d。",
                    result.downloaded,
                    result.already_present,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("今日姻缘：内置素材下载任务失败，可在后续启动时重试。")

    async def _maintenance_loop(self, notifications: NotificationService, retention: RetentionService) -> None:
        last_cleanup_day: int | None = None
        cleanup_retry_at = 0
        while True:
            try:
                now = int(time.time())
                expired = await notifications.expire_due(now)
                if expired:
                    logger.info("今日姻缘：已结束 %d 条超时赠送邀请。", expired)
                await notifications.send_pending()
                utc_day = now // 86400
                if utc_day != last_cleanup_day and now >= cleanup_retry_at:
                    try:
                        pruned = await asyncio.to_thread(retention.prune, now)
                        last_cleanup_day = utc_day
                        cleanup_retry_at = 0
                        total = sum(pruned.values())
                        if total:
                            logger.info("今日姻缘：历史清理完成，共移除 %d 项。", total)
                    except Exception:
                        cleanup_retry_at = now + 3600
                        logger.exception("今日姻缘：历史清理失败，将在稍后重试。")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("今日姻缘：邀请维护周期失败，将稍后重试。")
            await asyncio.sleep(5)
