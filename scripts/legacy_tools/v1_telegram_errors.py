"""Classify Telegram failures without leaking request URLs/tokens."""
import logging
from telegram.error import NetworkError, Conflict

logger = logging.getLogger(__name__)

async def handle_telegram_error(update, context):
    error = context.error
    if isinstance(error, Conflict):
        logger.error('Telegram polling conflict: check for another bot instance using this token.')
    elif isinstance(error, NetworkError):
        logger.warning('Telegram connection interrupted (%s). Polling retries automatically; local video state is unchanged.', type(error).__name__)
    else:
        logger.error('Telegram handler failed: %s; update_id=%s', type(error).__name__, getattr(update, 'update_id', None))
