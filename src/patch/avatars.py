"""XEP-0084 PEP avatar cache.

Subscribes to ``urn:xmpp:avatar:metadata`` PEP events on the open XMPP
stream. When a peer publishes a new avatar:

  1. The metadata stanza arrives carrying a SHA-1 hash + MIME type.
  2. If we don't already have a file for that hash on disk, send an
     IQ get for the corresponding ``urn:xmpp:avatar:data`` item.
  3. Decode the base64-wrapped image bytes and stash them under
     ``$XDG_CACHE_HOME/patch/avatars/<sha1>.<ext>``.
  4. Emit ``avatar-changed`` so the UI redraws affected rows.

For the JMP-first use case this fires mostly on direct XMPP peers —
the cheogram SMS gateway doesn't publish per-contact PEP avatars.
That's fine; ``Adw.Avatar`` already renders initial-on-color fallback
for the no-avatar case in the UI layer.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Optional

from gi.repository import GLib, GObject

from nbxmpp.namespaces import Namespace
from nbxmpp.structs import StanzaHandler

log = logging.getLogger(__name__)


_EXT_FOR_MIME = {
    "image/png":  "png",
    "image/jpeg": "jpg",
    "image/jpg":  "jpg",
    "image/gif":  "gif",
    "image/webp": "webp",
}


class AvatarManager(GObject.Object):
    __gtype_name__ = "PatchAvatarManager"

    __gsignals__ = {
        # bare JID whose avatar just landed on disk.
        "avatar-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, xmpp):
        super().__init__()
        self._xmpp = xmpp
        # bare jid → sha1 hex of the latest published avatar.
        self._by_jid: dict[str, str] = {}
        cache_dir = os.path.join(GLib.get_user_cache_dir(), "patch", "avatars")
        os.makedirs(cache_dir, exist_ok=True)
        self._cache_dir = cache_dir
        self._handler_registered = False
        # Auto-(re-)register the PubSub-event handler each time the
        # stream comes up. nbxmpp's Client rebinds its dispatcher
        # across smacks resume so re-registering is safe.
        self._xmpp.connect("state-changed", self._on_state_changed)

    def _on_state_changed(self, _xmpp, state):
        from patch import account as account_mod
        if state != account_mod.STATE_CONNECTED:
            self._handler_registered = False
            return
        client = self._xmpp._client  # noqa: SLF001 — internal access ok
        if client is None or self._handler_registered:
            return
        client.register_handler(StanzaHandler(
            name="message",
            callback=self._on_pubsub_event,
            ns=Namespace.PUBSUB_EVENT,
            priority=20,        # below UserAvatar's 16 — we observe
        ))
        self._handler_registered = True

    # -- nbxmpp handler --------------------------------------------------

    def _on_pubsub_event(self, _client, stanza, _properties):
        event = stanza.getTag("event", namespace=Namespace.PUBSUB_EVENT)
        if event is None:
            return
        items = event.getTag("items")
        if items is None:
            return
        node = items.getAttr("node") or ""
        if node != Namespace.AVATAR_METADATA:
            return
        item = items.getTag("item")
        if item is None:
            return
        sha = item.getAttr("id") or ""
        if not sha:
            return
        from_jid = stanza.getAttr("from") or ""
        bare = str(from_jid).split("/", 1)[0]
        if not bare:
            return
        metadata = item.getTag("metadata", namespace=Namespace.AVATAR_METADATA)
        # If the metadata has no <info/> children the peer has cleared
        # their avatar. Drop the mapping (we keep the cached file —
        # benign clutter that the disk cache may revisit later).
        info = metadata.getTag("info") if metadata is not None else None
        if info is None:
            self._by_jid.pop(bare, None)
            self.emit("avatar-changed", bare)
            return
        mime = info.getAttr("type") or "image/png"
        ext = _EXT_FOR_MIME.get(mime, "png")
        path = os.path.join(self._cache_dir, f"{sha}.{ext}")
        self._by_jid[bare] = sha
        if os.path.exists(path):
            # Cache hit; just signal the UI to render the (possibly
            # already known) avatar for this jid.
            self.emit("avatar-changed", bare)
            return
        log.info("avatar fetch: %s sha=%s", bare, sha)
        self._request_avatar_data(bare, sha, ext)

    # -- data request ----------------------------------------------------

    def _request_avatar_data(self, bare_jid: str, sha: str, ext: str) -> None:
        """Pull the actual image bytes for a metadata-announced hash."""
        client = self._xmpp._client  # noqa: SLF001
        if client is None:
            return
        try:
            module = client.get_module("UserAvatar")
        except Exception as exc:  # noqa: BLE001
            log.warning("UserAvatar module not available: %s", exc)
            return
        from nbxmpp.protocol import JID
        try:
            jid = JID.from_string(bare_jid)
        except Exception:  # noqa: BLE001
            return
        try:
            task = module.request_avatar_data(sha, jid=jid)
        except Exception as exc:  # noqa: BLE001
            log.warning("request_avatar_data threw: %s", exc)
            return
        # nbxmpp's iq_request_task decorator returns a Task we hook for
        # completion. The callback receives the task itself.
        task.add_done_callback(
            lambda t: self._on_data_arrived(t, bare_jid, sha, ext))

    def _on_data_arrived(self, task, bare_jid: str, sha: str, ext: str) -> None:
        try:
            avatar_data = task.finish()
        except Exception as exc:  # noqa: BLE001
            log.warning("avatar fetch failed for %s: %s", bare_jid, exc)
            return
        if avatar_data is None or not getattr(avatar_data, "data", None):
            return
        # avatar_data.data is base64-encoded text per XEP-0084 §4.2.
        try:
            blob = base64.b64decode(avatar_data.data)
        except Exception as exc:  # noqa: BLE001
            log.warning("avatar b64 decode failed for %s: %s", bare_jid, exc)
            return
        path = os.path.join(self._cache_dir, f"{sha}.{ext}")
        try:
            with open(path, "wb") as f:
                f.write(blob)
        except OSError as exc:
            log.warning("avatar write failed (%s): %s", path, exc)
            return
        log.info("avatar saved: %s -> %s (%d bytes)", bare_jid, path, len(blob))
        self.emit("avatar-changed", bare_jid)

    # -- public lookup ---------------------------------------------------

    def path_for(self, bare_jid: str) -> Optional[str]:
        """Return the on-disk path of the cached avatar, or None.

        Probes every supported extension because we only stash the SHA-1
        plus its declared MIME, and the metadata event may not have set
        a MIME (or may have changed between sessions)."""
        sha = self._by_jid.get(bare_jid)
        if not sha:
            return None
        for ext in _EXT_FOR_MIME.values():
            path = os.path.join(self._cache_dir, f"{sha}.{ext}")
            if os.path.exists(path):
                return path
        return None
