"""今日姻缘插件入口。"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from PIL import Image as PILImage

from .services.access import AccessService
from .services.catalog.core import CatalogService
from .services.command_replies import RuntimeReply
from .services.command_runtime import CommandRuntime
from .services.gameplay import GameplayService
from .services.host_config import import_legacy_host_settings
from .services.message_chain import bypass_host_quote, reply_chain
from .services.message_templates import IMAGE_TOKEN
from .services.notifications import NotificationService
from .services.onebot import OneBotAdapter
from .services.outgoing_images import prepare_outgoing_image
from .services.retention import RetentionService
from .services.statistics import StatisticsService
from .services.storage import SQLiteStorage
from .services.web_api import WebManager

PLUGIN_NAME = "astrbot_plugin_daily_bonds"
RESOURCES_DIR = Path(__file__).resolve().parent / "resources"


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
        data_dir = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
        data_dir.mkdir(parents=True, exist_ok=True)
        storage = SQLiteStorage(data_dir / "state.sqlite3")
        try:
            await asyncio.to_thread(storage.open)
            await asyncio.to_thread(import_legacy_host_settings, self.config, storage)
            settings, _ = await asyncio.to_thread(storage.get_settings)
            catalog = CatalogService(
                storage,
                RESOURCES_DIR / "defaults",
                data_dir / "media",
            )
            await asyncio.to_thread(catalog.seed_builtin_catalog, int(time.time()))
            self._storage = storage
            web_manager = WebManager(self.context, storage)
            web_manager.register()
            self._web_manager = web_manager
            notifications = NotificationService(self.context, storage)
            retention = RetentionService(storage, data_dir)
            stale_invites = await notifications.invalidate_pending_on_restart(
                int(time.time())
            )
            if stale_invites:
                logger.info(
                    "今日姻缘：重载时将 %d 条待处理赠送邀请标记为失效。", stale_invites
                )
            self._runtime = CommandRuntime(
                context=self.context,
                storage=storage,
                adapter=OneBotAdapter(self.context, storage),
                access=AccessService(),
                gameplay=GameplayService(storage),
                statistics=StatisticsService(storage),
                render_html=self.html_render,
            )
            web_manager.runtime_reset = self._runtime.reset
            web_manager.runtime = self._runtime
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
        except Exception:  # noqa: BLE001 - Handler 边界：隔离未知异常，不影响其他插件
            logger.exception("今日姻缘：处理群消息失败。")
            return
        if response:
            temporary_paths = []
            try:
                if isinstance(response, RuntimeReply) and IMAGE_TOKEN in response.text:
                    send_paths = []
                    for path in response.image_paths:
                        try:
                            send_path, temporary = await asyncio.to_thread(
                                prepare_outgoing_image, path
                            )
                        except (OSError, ValueError, PILImage.DecompressionBombError):
                            logger.exception("今日姻缘：缩放发送图片失败，使用原图。")
                            send_path, temporary = path, False
                        send_paths.append(send_path)
                        if temporary:
                            temporary_paths.append(send_path)
                    response = replace(response, image_paths=tuple(send_paths))
                chain = reply_chain(response, event)
                if chain and self._bypass_host_quote(response, chain, event):
                    # 直接发送不经过宿主结果装饰，宿主不会再补引用。
                    await event.send(MessageChain(chain))
                elif chain:
                    yield event.chain_result(chain)
            finally:
                for image_path in temporary_paths:
                    image_path.unlink(missing_ok=True)
                if isinstance(response, RuntimeReply):
                    for image_path in response.cleanup_paths:
                        image_path.unlink(missing_ok=True)

    def _bypass_host_quote(
        self, response: str | RuntimeReply, chain: list, event: AstrMessageEvent
    ) -> bool:
        text = response.text if isinstance(response, RuntimeReply) else response
        try:
            host_config = self.context.get_config(umo=event.unified_msg_origin)
        except Exception:  # noqa: BLE001 - 宿主接口边界：读取失败时交给宿主发送
            logger.warning("今日姻缘：读取宿主回复设置失败。", exc_info=True)
            return False
        return bypass_host_quote(
            chain, bool(getattr(text, "quote_source", False)), host_config
        )

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
        except Exception:  # noqa: BLE001 - 后台任务边界：记录后继续运行
            logger.exception("今日姻缘：内置素材下载任务失败，可在后续启动时重试。")

    async def _maintenance_loop(
        self, notifications: NotificationService, retention: RetentionService
    ) -> None:
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
                    except Exception:  # noqa: BLE001 - 历史清理失败，一小时后重试
                        cleanup_retry_at = now + 3600
                        logger.exception("今日姻缘：历史清理失败，将在稍后重试。")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 后台任务边界：记录后继续运行
                logger.exception("今日姻缘：邀请维护周期失败，将稍后重试。")
            await asyncio.sleep(5)
