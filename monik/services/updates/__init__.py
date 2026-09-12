"""Наблюдение за обновлениями операционной системы."""

from monik.services.updates.apt import AptPendingUpdates
from monik.services.updates.ports import PendingUpdate, PendingUpdatesSource
from monik.services.updates.service import PendingUpdatesNotifier, UpdateWatcher

__all__ = [
    "AptPendingUpdates",
    "PendingUpdate",
    "PendingUpdatesNotifier",
    "PendingUpdatesSource",
    "UpdateWatcher",
]
