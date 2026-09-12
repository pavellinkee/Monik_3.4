"""Наблюдение за обновлениями операционной системы."""

from monik.services.updates.apt import AptPendingUpdates
from monik.services.updates.apt_updater import AptSystemUpdater
from monik.services.updates.ports import (
    PendingUpdate,
    PendingUpdatesSource,
    SystemUpdater,
    SystemUpdateResult,
)
from monik.services.updates.service import PendingUpdatesNotifier, UpdateWatcher

__all__ = [
    "AptPendingUpdates",
    "AptSystemUpdater",
    "PendingUpdate",
    "PendingUpdatesNotifier",
    "PendingUpdatesSource",
    "SystemUpdateResult",
    "SystemUpdater",
    "UpdateWatcher",
]
