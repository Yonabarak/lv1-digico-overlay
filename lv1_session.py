"""
LV1 session file parser.

Reads the Waves eMotion LV1 SQLite database (.emo session files or the live
autosave file CurrentLV1.dat) and builds a mapping:

    (layer_idx: 0-7, slot_idx: 0-15)  →  digico_channel (1-based int)

Overlay policy (enforced everywhere):
    Preamp overlay is shown ONLY for Input track assignments (track_type=0)
    that have an MGB hardware route.  All other LV1 channel types — Group,
    Mon, FX, Matrix, Link, Aux, etc. — are always pass-through (no overlay).

Chain (Input tracks only):
    LV1 Page (layer_idx 0-7) + strip position (slot_idx 0-15)
        ↓  ovv_layer_track rows in Custom mode only (track_type=0); Factory sequential
    LV1 input channel index (0-based)
        ↓  routes table  (src_cluster_type=10 "Inputs" → dst_cluster_type=0 "Input")
    MGB hardware input index  (src_channel_index, 0-based)
        ↓  1:1 physical wiring
    DiGiCo channel number (1-based)
"""
from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# Live autosave paths LV1 keeps updated while running.
AUTOSAVE_PATHS: List[str] = [
    r"C:\Users\Public\Waves Audio\eMotion\Sessions\CurrentLV1.dat",
    r"D:\Users\Public\Waves Audio\eMotion\Sessions\CurrentLV1.dat",
]

# LV1 writes live state to CurrentLV1.dat; the user-visible name lives on the
# sibling .emo saved in the same folder with a matching mtime.
_AUTOSAVE_STEMS = frozenset({"currentlv1"})
_EMO_PAIR_MAX_DELTA_SEC = 120.0


def find_autosave() -> Optional[str]:
    """Return the first existing LV1 autosave path, or None."""
    for p in AUTOSAVE_PATHS:
        if os.path.exists(p):
            return p
    return None


class Lv1SessionParser:
    """
    Parses an LV1 SQLite session and exposes slot→DiGiCo channel lookups.

    Usage::

        parser = Lv1SessionParser(find_autosave())
        # DiGiCo channel for Layer 1 (idx=0), slot 3 (idx=2)
        ch = parser.get_digico_channel(0, 2)   # e.g. 3
        # All 16 channels for layer 2 (idx=1)
        channels = parser.get_layer_channels(1)  # [17, 18, … , 32]
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._factory_maps:  Dict[int, Dict[Tuple[int, int], int]] = {2: {}, 3: {}}
        self._custom_maps:   Dict[int, Dict[Tuple[int, int], int]] = {2: {}, 3: {}}
        self._factory_stereo_maps: Dict[int, Dict[Tuple[int, int], int]] = {2: {}, 3: {}}
        self._custom_stereo_maps:  Dict[int, Dict[Tuple[int, int], int]] = {2: {}, 3: {}}
        # Legacy aliases (Mixer 1) — updated on each refresh.
        self._factory_map:  Dict[Tuple[int, int], int] = {}
        self._custom_map:   Dict[Tuple[int, int], int] = {}
        self._factory_stereo: Dict[Tuple[int, int], int] = {}
        self._custom_stereo:  Dict[Tuple[int, int], int] = {}
        self._layer_names: List[str] = []
        self._name_map: Dict[int, str] = {}    # digico_ch (1-based) → LV1 channel name
        self._last_mtime: float = 0.0
        self._is_custom: bool = False          # kept for legacy compat
        self._track_col: Optional[str] = None  # cached ovv_layer_track column name
        self._track_mode_col: Optional[str] = None  # cached ovv_track_mode mode column
        self._track_mode_surface_col: Optional[str] = None
        self._track_mode_layer_col: Optional[str] = None
        self._mixer_layer_ids: Dict[int, List[int]] = {2: [], 3: []}
        self._custom_layer_ids_set: Dict[int, set] = {2: set(), 3: set()}
        self._factory_layer_ids: List[int] = []
        self._custom_layer_ids: List[int] = []
        self._layer_track_map: Dict[Tuple[int, int], int] = {}   # (layer_id, strip_idx) -> lv1_idx
        self._lv1_to_digico: Dict[int, int] = {}                 # lv1_idx -> digico_ch
        self._spill_groups: Dict[int, List[int]] = {}            # link group (1-16) -> lv1 idx
        self._session_name: str = ""
        self._routes_fingerprint: str = ""
        self._paired_emo_name: str = ""
        self._autosave_resolved_mtime: float = 0.0
        self._last_db_token: tuple = ()
        self.refresh()

    def _slot_count(self) -> int:
        n = 0
        for surface_id in self.MIXER_SURFACES:
            n += len(self._factory_maps.get(surface_id, {}))
            n += len(self._custom_maps.get(surface_id, {}))
        return n

    @classmethod
    def _is_preamp_track_type(cls, track_type: object) -> bool:
        """Return True only for Input — every other LV1 track type is excluded."""
        try:
            return int(track_type) == cls.TRACK_TYPE_INPUT
        except (TypeError, ValueError):
            return False

    def _resolve_preamp_digico(
        self,
        track_type: object,
        lv1_idx: object,
        lv1_to_digico: Optional[Dict[int, int]] = None,
    ) -> Optional[int]:
        """Map a custom-layer assignment to DiGiCo only when it is an Input + MGB."""
        if not self._is_preamp_track_type(track_type):
            return None
        try:
            idx = int(lv1_idx)
        except (TypeError, ValueError):
            return None
        routes = lv1_to_digico if lv1_to_digico is not None else self._lv1_to_digico
        digico = routes.get(idx)
        return int(digico) if digico is not None else None

    def _resolve_input_lv1_digico(self, lv1_idx: object) -> Optional[int]:
        """Map an LV1 input index to DiGiCo when MGB-patched (spill / factory paths)."""
        try:
            idx = int(lv1_idx)
        except (TypeError, ValueError):
            return None
        digico = self._lv1_to_digico.get(idx)
        return int(digico) if digico is not None else None

    @staticmethod
    def _normalize_surface(surface_id: int) -> int:
        return 3 if int(surface_id) == 3 else 2

    def _slot_maps(
        self, surface_id: int, is_custom: bool,
    ) -> Tuple[Dict[Tuple[int, int], int], Dict[Tuple[int, int], int]]:
        s = self._normalize_surface(surface_id)
        if is_custom:
            return self._custom_maps[s], self._custom_stereo_maps[s]
        return self._factory_maps[s], self._factory_stereo_maps[s]

    # ── public API ────────────────────────────────────────────────────────────

    def refresh(self) -> int:
        """Re-read the database and rebuild both factory and custom maps.

        Returns the total number of (layer, slot) pairs across both maps; 0
        on error.  Updates the cached DB change token.
        """
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA query_only = ON")
            self._build_map(conn)
            self._name_map = self._build_name_map(conn)
            self._session_name = self._read_session_name(conn, self.db_path, self)
            self._spill_groups = self._build_spill_groups(conn)
            conn.close()
            self._note_db_token()
            total = self._slot_count()
            log.info("session updated: m1=factory=%d custom=%d m2=factory=%d custom=%d names=%d",
                     len(self._factory_maps[2]), len(self._custom_maps[2]),
                     len(self._factory_maps[3]), len(self._custom_maps[3]),
                     total)
            return total
        except Exception as exc:
            log.warning("session refresh error: %s", exc)
            return 0

    def refresh_if_changed(self) -> Tuple[bool, int]:
        """Re-parse only when the LV1 DB or its WAL changed.

        LV1 uses SQLite WAL mode — routing edits append to ``{db}-wal`` while
        the main file mtime may stay frozen until checkpoint.  We track WAL
        size as well as mtime so multiple patches within the same second are
        not missed on Windows.

        Returns (changed: bool, slot_count: int).
        ``changed`` is True when the mapping was re-built.
        """
        if not self._db_token_changed():
            return False, self._slot_count()

        old_factory = {s: dict(self._factory_maps[s]) for s in self.MIXER_SURFACES}
        old_custom = {s: dict(self._custom_maps[s]) for s in self.MIXER_SURFACES}
        old_names   = dict(self._name_map)
        old_session = self._session_name
        count       = self.refresh()
        routing_changed = any(
            self._factory_maps[s] != old_factory[s] or self._custom_maps[s] != old_custom[s]
            for s in self.MIXER_SURFACES
        )
        names_changed   = self._name_map != old_names
        session_changed = self._session_name != old_session
        return routing_changed or names_changed or session_changed, count

    def get_current_layer(self) -> int:
        """Read the currently active layer index (0-based) directly from the DB.

        Uses surface=2, property=14 (eProp_LayersPage).  Returns 0 on any error
        so the overlay stays on a valid layer.
        """
        try:
            conn = sqlite3.connect(self.db_path)
            row = conn.execute(
                "SELECT value FROM surface_property_int"
                " WHERE surface=2 AND property=14"
            ).fetchone()
            conn.close()
            return int(row[0]) if row else 0
        except Exception:
            return 0

    def get_current_layer_and_mode(self) -> tuple:
        """Return (layer_idx: int, is_custom: bool) from a single DB open."""
        try:
            conn = sqlite3.connect(self.db_path)
            rows = conn.execute(
                "SELECT property, value FROM surface_property_int"
                " WHERE surface=2 AND property IN (12, 14)"
            ).fetchall()
            conn.close()
            props = {p: v for p, v in rows}
            return int(props.get(14, 0)), bool(props.get(12, 0) == 1)
        except Exception:
            return 0, False

    # eProp_LayersSection values (surface=2, property=13).
    # LV1 defaults to Input mode (0) on startup.  Other modes are DYN/EQ,
    # Rack, Route, and Aux sends — exact DB values TBD but non-zero.
    SECTION_INPUT = 0   # Input Layer Mode (shows preamp controls)
    SECTION_SPILL = 2   # Spill mode UI (eProp_LayersSection during spill page 1)
    TRACK_MODE_NORMAL = 0
    TRACK_MODE_ALL = 1
    TRACK_TYPE_INPUT = 0   # only track_type that may show a preamp overlay
    PROP_TRACK_LINK_TYPE = 9     # eProp_TrackLinkType
    PROP_TRACK_LINK_INDEX = 10    # eProp_TrackLinkIndex
    PROP_LAYERS_PAGE = 14         # eProp_LayersPage — active page (factory/custom page; spill sub-page in spill)
    PROP_VIEW_ALL = 17          # eProp_ViewAll — 1 while ALL mode is active
    PROP_SEL_LINK_DCA = 1066      # eProp_SelLinkOnDCATouch — lives on surface 1

    def get_ui_state(self, surface_id: int = 2) -> tuple:
        """Return (layer_idx, is_custom, section, section_page) in a single DB open.

        ``surface_id`` selects the LV1 mixer surface to interrogate:
          2 = Mixer 1 tab  (channels 1-16 in Factory mode)
          3 = Mixer 2 tab  (channels 17-32 in Factory mode)

        ``section`` is the raw eProp_LayersSection value:
          0 = Input Layer Mode  (preamp controls visible)
          other values = DYN/EQ / Rack / Route / Aux modes

        ``layer_idx`` is the page index (0 = Page 1, 1 = Page 2, …).
        """
        try:
            conn = sqlite3.connect(self.db_path)
            rows = conn.execute(
                "SELECT property, value FROM surface_property_int"
                " WHERE surface=? AND property IN (12, 13, 14, 15)",
                (surface_id,),
            ).fetchall()
            conn.close()
            props = {p: v for p, v in rows}
            layer        = int(props.get(14, 0))
            section      = int(props.get(13, 0))
            section_page = int(props.get(15, 0))
            prop12_raw   = int(props.get(12, 0))
            custom       = (prop12_raw == 1)
            return layer, custom, section, section_page, prop12_raw
        except Exception:
            return 0, False, -1, -1, 0   # unknown section → overlay will hide

    def get_mode_context(self, surface_id: int = 2) -> dict:
        """Return LV1 mode context for active surface/mixer page.

        Shape:
            {
                "surface": int,
                "layer": int,
                "is_custom": bool,
                "section": int,
                "section_page": int,
                "track_mode": object,   # raw DB value when available
                "is_all": bool,
            }
        """
        try:
            layer, is_custom, section, section_page, prop12_raw = self.get_ui_state(surface_id)
            track_mode = self._read_track_mode(surface_id, layer, is_custom)
            is_all = self._track_mode_flags(track_mode)[0] or self._read_view_all(surface_id)
            # Known prop12 values that mean "non-input, hide overlay":
            #   5 = Factory MIX (80-ch config and similar extended configs)
            # All other values (0=factory-64ch, 1=custom, 4=factory-INPUT-80ch,
            # and any unknown future values) default to showing the overlay.
            _NON_INPUT_LAYER_TYPES = {5}
            is_input_layer = prop12_raw not in _NON_INPUT_LAYER_TYPES
            return {
                "surface": int(surface_id),
                "layer": int(layer),
                "is_custom": bool(is_custom),
                "section": int(section),
                "section_page": int(section_page),
                "track_mode": track_mode,
                "link_group": self._read_link_group_index(surface_id),
                "is_all": is_all,
                "is_spill": False,
                "is_input_layer": is_input_layer,
                "layer_type": prop12_raw,
            }
        except Exception:
            return {
                "surface": int(surface_id),
                "layer": 0,
                "is_custom": False,
                "section": -1,
                "section_page": -1,
                "track_mode": None,
                "link_group": 0,
                "is_all": False,
                "is_spill": False,
                "is_input_layer": True,
                "layer_type": 0,
            }

    def get_session_name(self) -> str:
        """Return the user-visible LV1 session name."""
        if self._session_name:
            return self._session_name
        stem = Path(self.db_path).stem
        if stem.lower() in _AUTOSAVE_STEMS:
            reg_name = self._read_lv1_recent_session()
            if reg_name:
                return reg_name
        if stem.lower() not in _AUTOSAVE_STEMS:
            return stem
        paired = self._find_paired_emo_name(self.db_path)
        return paired or stem

    @staticmethod
    def _compute_routes_fingerprint(conn: sqlite3.Connection) -> str:
        """Stable digest of LV1 input routing — matches autosave to its .emo file."""
        try:
            rows = conn.execute(
                "SELECT src_cluster_type_index, src_channel_index, dst_cluster_type_index"
                " FROM routes"
                " ORDER BY src_cluster_type_index, src_channel_index, dst_cluster_type_index"
            ).fetchall()
            return hashlib.sha256(repr(rows).encode()).hexdigest()
        except Exception:
            return ""

    def _resolve_autosave_display_name(self, conn: sqlite3.Connection) -> str:
        """Map CurrentLV1.dat to the user-visible .emo session name."""
        ref_mtime = self._effective_mtime(self.db_path)
        fp = self._compute_routes_fingerprint(conn)
        if (
            fp
            and fp == self._routes_fingerprint
            and self._paired_emo_name
            and abs(ref_mtime - self._autosave_resolved_mtime) < 0.001
        ):
            return self._paired_emo_name

        self._routes_fingerprint = fp
        self._autosave_resolved_mtime = ref_mtime
        self._paired_emo_name = ""
        if fp:
            self._paired_emo_name = self._find_emo_by_routes_fingerprint(fp, ref_mtime)
        if not self._paired_emo_name:
            self._paired_emo_name = self._find_paired_emo_name(self.db_path)
        return self._paired_emo_name

    def _find_emo_by_routes_fingerprint(
        self, fingerprint: str, ref_mtime: Optional[float] = None,
    ) -> str:
        """Return .emo stem whose routes table matches *fingerprint*."""
        if not fingerprint:
            return ""
        sessions_dir = Path(self.db_path).parent
        if not sessions_dir.is_dir():
            return ""

        matches: List[Path] = []
        try:
            for emo in sessions_dir.glob("*.emo"):
                try:
                    conn = sqlite3.connect(f"file:{emo}?mode=ro", uri=True)
                    fp = self._compute_routes_fingerprint(conn)
                    conn.close()
                    if fp == fingerprint:
                        matches.append(emo)
                except Exception:
                    continue
        except Exception:
            return ""

        if not matches:
            return ""
        if len(matches) == 1:
            return matches[0].stem

        if ref_mtime is None:
            ref_mtime = self._effective_mtime(self.db_path)
        best = min(
            matches,
            key=lambda p: (
                abs(self._effective_mtime(str(p)) - ref_mtime),
                -self._effective_mtime(str(p)),
            ),
        )
        return best.stem

    def get_digico_channel(self, layer_idx: int, slot_idx: int,
                           is_custom: bool = False,
                           surface_id: int = 2) -> Optional[int]:
        """Return primary DiGiCo channel (1-based) for given layer/slot, or None."""
        m, _ = self._slot_maps(surface_id, is_custom)
        return m.get((layer_idx, slot_idx))

    def get_secondary_digico_channel(self, layer_idx: int, slot_idx: int,
                                     is_custom: bool = False,
                                     surface_id: int = 2) -> Optional[int]:
        """Return secondary (R-side) DiGiCo channel for stereo slots, or None for mono."""
        _, m = self._slot_maps(surface_id, is_custom)
        return m.get((layer_idx, slot_idx))

    def get_layer_channels(self, layer_idx: int,
                           is_custom: bool = False,
                           surface_id: int = 2) -> List[Optional[int]]:
        """Return a list of 16 DiGiCo channels for a layer (None = unpatched)."""
        m, _ = self._slot_maps(surface_id, is_custom)
        return [m.get((layer_idx, s)) for s in range(16)]

    def get_spill_slot_channels(
        self,
        link_group: int,
        surface_id: int = 2,
        spill_page: int = 0,
    ) -> List[Optional[int]]:
        """Return 16 DiGiCo channels for spill layout on *link_group* (1-16).

        LV1 spill row layout (0-based strip indices):
          strip 0 — link anchor strip (no patch; always pass-through)
          strips 1-15 — one link-group member each for the active spill page

        Each strip maps to the corresponding link member in order.  Members
        without an MGB patch are None (overlay pass-through), preserving strip
        alignment when a link contains non-MGB channels.

        Links with more than 15 members continue on spill page 2, 3, …
        (15 members per page).  *spill_page* is 0-based (Page 1 = 0).
        """
        slots: List[Optional[int]] = [None] * 16
        if link_group < 1:
            return slots
        lv1_members = self._spill_groups.get(int(link_group), [])
        if not lv1_members:
            return slots

        page = max(0, int(spill_page))
        per_page = 15
        offset = page * per_page
        page_members = lv1_members[offset:offset + per_page]

        for i, lv1_idx in enumerate(page_members, start=1):
            digico_ch = self._resolve_input_lv1_digico(lv1_idx)
            slots[i] = digico_ch
        return slots

    def read_spill_page(self, surface_id: int = 2, ui_layer: int = -1) -> int:
        """Return 0-based spill page index from LV1 LayersPage (prop 14).

        Page 1 = 0, Page 2 = 1, etc.  Always reads prop 14 live so the overlay
        matches whichever spill page LV1 is showing.
        """
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA query_only = ON")
            row = conn.execute(
                "SELECT value FROM surface_property_int"
                " WHERE surface=? AND property=?",
                (int(surface_id), self.PROP_LAYERS_PAGE),
            ).fetchone()
            conn.close()
            if row is not None:
                return max(0, int(row[0]))
        except Exception:
            pass
        if ui_layer >= 0:
            return max(0, int(ui_layer))
        return 0

    def get_secondary_for_digico(self, digico_ch: int) -> Optional[int]:
        """Return R-side DiGiCo channel for a primary channel, if stereo."""
        try:
            ch = int(digico_ch)
        except (TypeError, ValueError):
            return None
        for primary, stereo in (
            (self._factory_maps[2], self._factory_stereo_maps[2]),
            (self._custom_maps[2], self._custom_stereo_maps[2]),
            (self._factory_maps[3], self._factory_stereo_maps[3]),
            (self._custom_maps[3], self._custom_stereo_maps[3]),
        ):
            for (layer, slot), p in primary.items():
                if p == ch:
                    return stereo.get((layer, slot))
        return None

    def _get_link_digico_members(self, link_group: int) -> List[int]:
        """Ordered DiGiCo channels (1-based) for a link group."""
        lv1_members = self._spill_groups.get(int(link_group), [])
        packed: List[int] = []
        for lv1_idx in lv1_members:
            digico_ch = self._lv1_to_digico.get(int(lv1_idx))
            if digico_ch is not None:
                packed.append(int(digico_ch))
        return packed

    def get_digico_name_map(self) -> Dict[int, str]:
        """Return {digico_ch_1based: lv1_channel_name} for all MGB-patched channels."""
        return dict(self._name_map)

    @staticmethod
    def _build_name_map(conn: sqlite3.Connection) -> Dict[int, str]:
        """Build digico_ch (1-based) → LV1 channel name from routes + snapshot_chainer.

        When multiple LV1 channels share the same DiGiCo preamp (double-patch),
        the lowest dst_cluster_type_index (strip index) determines the name.
        """
        try:
            rows = conn.execute("""
                SELECT r.src_channel_index, r.dst_cluster_type_index
                FROM routes r
                JOIN device d ON d.assign = r.src_cluster_type_index
                JOIN device_name dn ON dn.mac = d.mac
                WHERE r.src_cluster_type = 10
                  AND r.dst_cluster_type = 0
                  AND (dn.name LIKE '%MGB%' OR dn.name LIKE '%DiGiGrid%')
                ORDER BY r.dst_cluster_type_index ASC
            """).fetchall()
            digico_to_lv1: Dict[int, int] = {}
            for src, dst in rows:
                digico_ch = src + 1
                # Lowest strip index wins — first row per digico_ch is kept.
                if digico_ch not in digico_to_lv1:
                    digico_to_lv1[digico_ch] = dst

            name_rows = conn.execute("""
                SELECT o.obj_index, sc.name
                FROM snapshot_chainer sc
                JOIN chainer c ON c.obj_id = sc.chainer_id
                JOIN object o ON o.id = c.obj_id
                WHERE sc.snapshot_id = -1 AND o.obj_type = 0
            """).fetchall()
            lv1_names: Dict[int, str] = {slot: name for slot, name in name_rows if name}

            return {
                digico_ch: lv1_names[lv1_slot]
                for digico_ch, lv1_slot in digico_to_lv1.items()
                if lv1_slot in lv1_names
            }
        except Exception:
            return {}

    @property
    def layer_names(self) -> List[str]:
        """Human-readable layer names from ovv_layer (e.g. 'Page-1')."""
        return list(self._layer_names)

    @property
    def n_layers(self) -> int:
        return len(self._layer_names)

    # ── internal ──────────────────────────────────────────────────────────────

    # ovv_layer.surface_type = LV1 mixer surface id:
    #   2 = Mixer 1 (eSurface_OVV1),  3 = Mixer 2 (eSurface_OVV2)
    # Factory vs Custom is runtime per-mixer (surface_property_int property 12).
    MIXER_SURFACES = (2, 3)
    SURFACE_MIXER1 = 2
    SURFACE_MIXER2 = 3

    def _build_map(self, conn: sqlite3.Connection) -> None:
        """Build per-mixer factory/custom slot maps from ovv_layer + ovv_layer_track.

        Primary maps:   (layer_idx, slot_idx) → DiGiCo ch (1-based), L/primary for stereo.
        Stereo maps:    (layer_idx, slot_idx) → DiGiCo ch (1-based), R/secondary — only
                        present for slots that have a stereo pair; absent for mono slots.
        """

        # --- MGB device assign indices (shared by both maps) ---
        try:
            mgb_assigns: list = [
                row[0] for row in conn.execute("""
                    SELECT DISTINCT d.assign
                    FROM device d
                    JOIN device_name dn ON dn.mac = d.mac
                    WHERE dn.name LIKE '%MGB%'
                       OR dn.name LIKE '%DiGiGrid%'
                """).fetchall()
            ]
        except Exception:
            mgb_assigns = []

        # --- Authoritative input-channel count from config_info -----------------
        # config_info.track_type == TRACK_TYPE_INPUT (0) → num_tracks = how many
        # real input channels exist in the CURRENT session.  Routes targeting
        # dst_cluster_type_index >= this value are stale leftovers from a previous
        # larger config (e.g. 80ch routes persisting after switching to 64ch).
        try:
            row = conn.execute(
                "SELECT num_tracks FROM config_info WHERE track_type=?",
                (self.TRACK_TYPE_INPUT,),
            ).fetchone()
            max_input_channels: Optional[int] = int(row[0]) if row else None
        except Exception:
            max_input_channels = None

        # --- routes: LV1 ch index (0-based) → DiGiCo ch (1-based) ---
        if mgb_assigns:
            placeholders = ",".join("?" * len(mgb_assigns))
            route_rows = conn.execute(f"""
                SELECT src_channel_index, dst_cluster_type_index
                FROM routes
                WHERE src_cluster_type = 10
                  AND dst_cluster_type = 0
                  AND src_cluster_type_index IN ({placeholders})
                ORDER BY dst_cluster_type_index ASC, src_channel_index ASC
            """, mgb_assigns).fetchall()
        else:
            route_rows = conn.execute("""
                SELECT src_channel_index, dst_cluster_type_index
                FROM routes
                WHERE src_cluster_type = 10 AND dst_cluster_type = 0
                ORDER BY dst_cluster_type_index ASC, src_channel_index ASC
            """).fetchall()

        # Stereo LV1 channels: chainer.num_inputs == 2.
        # chainer.obj_id → object.id → object.obj_index = LV1 ch index (0-based).
        try:
            stereo_lv1_indices: set = {
                row[0] for row in conn.execute("""
                    SELECT o.obj_index
                    FROM chainer c
                    JOIN object o ON o.id = c.obj_id
                    WHERE c.num_inputs = 2 AND o.obj_type = 0
                """).fetchall()
            }
        except Exception:
            stereo_lv1_indices = set()

        # For stereo LV1 channels both the L and R MGB sources share the same
        # dst_cluster_type_index.  Build two dicts:
        #   lv1_to_digico   – primary (lowest src = L side) DiGiCo ch (1-based)
        #   lv1_to_digico_r – secondary (highest src = R side) DiGiCo ch, stereo only
        lv1_to_digico:   Dict[int, int] = {}
        lv1_to_digico_r: Dict[int, int] = {}
        for src, dst in route_rows:
            dst = int(dst)
            src = int(src)
            # Skip routes targeting LV1 indices beyond the current config's
            # input-channel count — these are stale patches from a prior session
            # that LV1 does not clear when switching to a smaller config.
            if max_input_channels is not None and dst >= max_input_channels:
                continue
            if dst not in lv1_to_digico:
                lv1_to_digico[dst]   = src + 1
                lv1_to_digico_r[dst] = src + 1
            elif src < lv1_to_digico[dst] - 1:
                lv1_to_digico_r[dst] = lv1_to_digico[dst]
                lv1_to_digico[dst]   = src + 1
            elif src > lv1_to_digico_r[dst] - 1:
                lv1_to_digico_r[dst] = src + 1
        lv1_stereo_r: Dict[int, int] = {
            dst: r for dst, r in lv1_to_digico_r.items()
            if r != lv1_to_digico.get(dst) and dst in stereo_lv1_indices
        }
        self._lv1_to_digico = {int(k): int(v) for k, v in lv1_to_digico.items()}

        # --- ovv_layer_track (custom assignments for both mixers) ---
        track_rows: List[Tuple[int, int, int, int]] = []
        try:
            raw = conn.execute(
                "SELECT layer_id, strip_index, track_type, track_index"
                " FROM ovv_layer_track"
            ).fetchall()
            track_rows = [
                (int(lid), int(strip), int(tt), int(tidx))
                for lid, strip, tt, tidx in raw
                if strip is not None and tidx is not None
            ]
        except Exception as exc:
            log.warning("ovv_layer_track read error: %s", exc)

        self._layer_track_map = {
            (int(lid), int(strip)): int(lv1_idx)
            for lid, strip, track_type, lv1_idx in track_rows
            if self._is_preamp_track_type(track_type)
            and strip is not None and lv1_idx is not None
        }

        for surface_id in self.MIXER_SURFACES:
            layer_rows = conn.execute(
                "SELECT id, name FROM ovv_layer WHERE surface_type=? ORDER BY id",
                (surface_id,),
            ).fetchall()
            layer_ids = [int(r[0]) for r in layer_rows]
            self._mixer_layer_ids[surface_id] = layer_ids
            layer_id_to_idx = {lid: i for i, lid in enumerate(layer_ids)}
            surface_layers = set(layer_ids)
            layers_with_rows = {
                int(lid) for lid, _, _, _ in track_rows if int(lid) in surface_layers
            }
            self._custom_layer_ids_set[surface_id] = layers_with_rows

            factory_map: Dict[Tuple[int, int], int] = {}
            factory_stereo: Dict[Tuple[int, int], int] = {}
            # Both Mixer 1 and Mixer 2 are independent views of the same LV1 input
            # channel list starting from ch 1 (index 0).  Factory page N on either
            # mixer shows LV1 channels (N*16)+1 … (N+1)*16.  There is no tab_base
            # offset between the two mixers in factory mode.
            tab_base = 0
            # Strictly route-driven: a strip only gets a preamp overlay when its
            # LV1 input index has an MGB route.  Stale routes beyond the config's
            # input-channel count were already filtered out of lv1_to_digico above.
            for layer_idx in range(len(layer_ids)):
                for slot_idx in range(16):
                    lv1_ch_idx = tab_base + layer_idx * 16 + slot_idx
                    digico_ch = lv1_to_digico.get(lv1_ch_idx)
                    if digico_ch is not None:
                        factory_map[(layer_idx, slot_idx)] = digico_ch
                        r_ch = lv1_stereo_r.get(lv1_ch_idx)
                        if r_ch is not None:
                            factory_stereo[(layer_idx, slot_idx)] = r_ch

            custom_map: Dict[Tuple[int, int], int] = {}
            custom_stereo: Dict[Tuple[int, int], int] = {}
            if track_rows and layer_ids:
                for layer_id, strip_idx, track_type, lv1_ch_idx in track_rows:
                    if int(layer_id) not in surface_layers:
                        continue
                    layer_idx = layer_id_to_idx.get(int(layer_id))
                    if layer_idx is None:
                        continue
                    digico_ch = self._resolve_preamp_digico(
                        track_type, lv1_ch_idx, lv1_to_digico,
                    )
                    if digico_ch is None:
                        continue
                    custom_map[(layer_idx, strip_idx)] = digico_ch
                    r_ch = lv1_stereo_r.get(int(lv1_ch_idx))
                    if r_ch is not None:
                        custom_stereo[(layer_idx, strip_idx)] = r_ch

            # Custom mode: only explicitly assigned Input strips get overlay.
            # Unassigned pages and non-input types stay pass-through (no factory fill).

            self._factory_maps[surface_id] = factory_map
            self._custom_maps[surface_id] = custom_map
            self._factory_stereo_maps[surface_id] = factory_stereo
            self._custom_stereo_maps[surface_id] = custom_stereo

        self._layer_names = [
            r[0] for r in conn.execute(
                "SELECT name FROM ovv_layer WHERE surface_type=? ORDER BY id",
                (self.SURFACE_MIXER1,),
            ).fetchall()
        ]

        # Legacy aliases (Mixer 1) for older call sites.
        self._factory_map = self._factory_maps[self.SURFACE_MIXER1]
        self._custom_map = self._custom_maps[self.SURFACE_MIXER1]
        self._factory_stereo = self._factory_stereo_maps[self.SURFACE_MIXER1]
        self._custom_stereo = self._custom_stereo_maps[self.SURFACE_MIXER1]
        self._factory_layer_ids = self._mixer_layer_ids[self.SURFACE_MIXER1]
        self._custom_layer_ids = self._mixer_layer_ids[self.SURFACE_MIXER1]

    def _build_spill_groups(self, conn: sqlite3.Connection) -> Dict[int, List[int]]:
        """Map link group index (1-16) to sorted 0-based LV1 channel indices."""
        groups: Dict[int, List[int]] = {}
        try:
            rows = conn.execute(
                "SELECT sc.id, sc.name FROM snapshot_chainer sc"
                " WHERE sc.snapshot_id=-1 AND sc.name LIKE 'Link %'"
            ).fetchall()
            for chainer_id, name in rows:
                parts = str(name).strip().split()
                if len(parts) != 2 or parts[0] != "Link":
                    continue
                try:
                    link_n = int(parts[1])
                except ValueError:
                    continue
                members = conn.execute(
                    "SELECT sa.src_id FROM snapshot_assignment sa"
                    " WHERE sa.snapshot_id=-1 AND sa.dst_id=?"
                    " ORDER BY sa.src_id",
                    (int(chainer_id),),
                ).fetchall()
                lv1_indices = sorted(int(r[0]) - 1 for r in members if r[0] is not None)
                if lv1_indices:
                    groups[link_n] = lv1_indices
        except Exception:
            pass
        return groups

    def _read_track_mode(self, surface_id: int, layer_idx: int, is_custom: bool) -> object:
        """Best-effort read of live track mode.

        Priority:
          1) surface_property_int property=17 (live UI state)
          2) ovv_track_mode.* fallback (schema-tolerant)
        """
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA query_only = ON")
        except Exception:
            return None
        try:
            # Live UI state for ALL on tested LV1 builds.
            try:
                row = conn.execute(
                    "SELECT value FROM surface_property_int"
                    " WHERE surface=? AND property=?",
                    (int(surface_id), self.PROP_VIEW_ALL),
                ).fetchone()
                if row is not None and int(row[0]) == self.TRACK_MODE_ALL:
                    return self.TRACK_MODE_ALL
            except Exception:
                pass

            cols = [r[1] for r in conn.execute("PRAGMA table_info(ovv_track_mode)").fetchall()]
            if not cols:
                return None

            if self._track_mode_col is None:
                self._track_mode_col = next(
                    (c for c in cols if c in ("track_mapping_type", "mapping_type", "mode", "type")),
                    "",
                )
            mode_col = self._track_mode_col or ""
            if not mode_col:
                return None
            if self._track_mode_surface_col is None:
                self._track_mode_surface_col = next(
                    (c for c in cols if c in ("surface", "surface_id", "surface_type")),
                    "",
                )
            if self._track_mode_layer_col is None:
                self._track_mode_layer_col = next(
                    (c for c in cols if c in ("layer", "layer_id", "page", "page_idx", "layer_idx")),
                    "",
                )
            layer_ids = self._mixer_layer_ids.get(int(surface_id), [])
            layer_id = layer_ids[layer_idx] if 0 <= layer_idx < len(layer_ids) else None
            where_parts: List[str] = []
            params: List[object] = []
            if self._track_mode_surface_col:
                where_parts.append(f"{self._track_mode_surface_col}=?")
                params.append(int(surface_id))
            if self._track_mode_layer_col and layer_id is not None:
                where_parts.append(f"{self._track_mode_layer_col}=?")
                params.append(int(layer_id))
            where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
            row2 = conn.execute(
                f"SELECT {mode_col} FROM ovv_track_mode{where_sql}"
                " ORDER BY ROWID DESC LIMIT 1",
                params,
            ).fetchone()
            if row2 is not None:
                return row2[0]
            row3 = conn.execute(
                f"SELECT {mode_col} FROM ovv_track_mode ORDER BY ROWID DESC LIMIT 1"
            ).fetchone()
            if row3 is not None:
                return row3[0]
            return None
        except Exception:
            return None
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _read_view_all(self, surface_id: int = 2) -> bool:
        """Return True when eProp_ViewAll indicates ALL mode is active."""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA query_only = ON")
            row = conn.execute(
                "SELECT value FROM surface_property_int"
                " WHERE surface=? AND property=?",
                (int(surface_id), self.PROP_VIEW_ALL),
            ).fetchone()
            conn.close()
            return row is not None and int(row[0]) == self.TRACK_MODE_ALL
        except Exception:
            return False

    @classmethod
    def _track_mode_flags(cls, raw_mode: object) -> Tuple[bool, bool]:
        """Return (is_all, is_spill) from raw ovv_track_mode value."""
        if raw_mode is None:
            return False, False
        try:
            mode_i = int(raw_mode)
            return mode_i == cls.TRACK_MODE_ALL, False
        except Exception:
            pass
        mode_s = str(raw_mode).strip().lower()
        return ("all" in mode_s), False

    @staticmethod
    def _sanitize_link_index(value: object) -> int:
        """LV1 uses 4294967295 (-1) for unset; valid link indices are 1-16."""
        try:
            v = int(value)
        except (TypeError, ValueError):
            return 0
        if v >= 2 ** 31:
            return 0
        if 12 <= v <= 27:
            return v - 11
        if v < 1 or v > 16:
            return 0
        return v

    @staticmethod
    def prop10_raw_to_link_group(raw: object) -> int:
        """Map prop10 to link group when a link key is selected (not in spill)."""
        try:
            v = int(raw)
        except (TypeError, ValueError):
            return 0
        if v >= 2 ** 31:
            return 0
        if 12 <= v <= 27:
            return v - 11
        if 1 <= v <= 16:
            return v
        return 0

    @staticmethod
    def prop10_spill_to_link_group(raw: object) -> int:
        """Map prop10 to link group during spill (0-based: 0=Link 1, 1=Link 2, …).

        Do not use the TrackLinkType band (12–27) here — prop10=12 is Link 13,
        not Link 1 (12 - 11).
        """
        try:
            v = int(raw)
        except (TypeError, ValueError):
            return 0
        if v >= 2 ** 31:
            return 0
        if 0 <= v <= 15:
            return v + 1
        if v == 16:
            return 16
        return 0

    def read_track_link_index_raw(self, surface_id: int = 2) -> int:
        """Return raw eProp_TrackLinkIndex (prop 10) without link-group conversion."""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA query_only = ON")
            row = conn.execute(
                "SELECT value FROM surface_property_int"
                " WHERE surface=? AND property=?",
                (int(surface_id), self.PROP_TRACK_LINK_INDEX),
            ).fetchone()
            conn.close()
            if row is not None:
                return int(row[0])
        except Exception:
            pass
        return 0

    def read_track_link_index(self, surface_id: int = 2) -> int:
        """Return selected link group (1-16) from prop 10 when a link key is held."""
        return self.prop10_raw_to_link_group(
            self.read_track_link_index_raw(surface_id),
        )

    def resolve_spill_link_from_samples(self, samples: List[int]) -> int:
        """Pick link group from prop-10 samples (ignores zeros — unset during spill)."""
        decoded: List[int] = []
        for raw in samples:
            group = self.prop10_spill_to_link_group(raw)
            if group > 0 and group in self._spill_groups:
                decoded.append(group)
        if decoded:
            return int(decoded[-1])
        return 0

    def resolve_spill_link_group_after_capture(
        self,
        surface_id: int,
        baseline_raw: int,
        pulse_values: List[int],
        prev_link: int,
        pre_spill_digicos: Optional[List[Optional[int]]] = None,
        selected_link: int = 0,
    ) -> int:
        """Resolve spilled link — prop10 is usually 0 during spill; use selected_link."""
        if selected_link > 0 and selected_link in self._spill_groups:
            return int(selected_link)

        group = self.resolve_spill_link_from_samples(list(pulse_values))
        if group > 0:
            return group

        group = self.prop10_raw_to_link_group(baseline_raw)
        if group > 0 and group in self._spill_groups:
            return group

        if pre_spill_digicos:
            visible = [c for c in pre_spill_digicos if c is not None]
            if visible and len(visible) < 12:
                overlap = self.infer_link_group_from_digico_overlap(visible)
                if overlap > 0:
                    return overlap

        return 0

    @staticmethod
    def _sanitize_link_from_type(value: object) -> int:
        """Infer link index from eProp_TrackLinkType when TrackLinkIndex is unset."""
        try:
            v = int(value)
        except (TypeError, ValueError):
            return 0
        if v >= 2 ** 31:
            return 0
        # Live capture: Link 1 → TrackLinkType 12 on surface 2.
        if 12 <= v <= 27:
            return v - 11
        return 0

    def read_spill_link_from_chainer(self) -> int:
        """Return link group (1-16) from snapshot_chainer selected_slot if set."""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA query_only = ON")
            rows = conn.execute(
                "SELECT name, selected_slot FROM snapshot_chainer"
                " WHERE snapshot_id=-1 AND name LIKE 'Link %'"
            ).fetchall()
            conn.close()
            for name, slot in rows:
                if int(slot or 0) <= 0:
                    continue
                parts = str(name).strip().split()
                if len(parts) == 2 and parts[0] == "Link":
                    try:
                        return int(parts[1])
                    except ValueError:
                        continue
        except Exception:
            pass
        return 0

    def get_layer_channels_by_page(
        self, page: int, is_custom: bool = False, surface_id: int = 2,
    ) -> List[Optional[int]]:
        """Return DiGiCo channels for a UI page (eProp_LayersPage value)."""
        try:
            page_idx = int(page)
        except (TypeError, ValueError):
            page_idx = 0
        s = self._normalize_surface(surface_id)
        layer_ids = self._mixer_layer_ids.get(s, [])
        if is_custom and 0 <= page_idx < len(layer_ids):
            layer_id = layer_ids[page_idx]
            slots: List[Optional[int]] = [None] * 16
            for (lid, strip), lv1_idx in self._layer_track_map.items():
                if int(lid) != int(layer_id):
                    continue
                if strip is None or not (0 <= int(strip) <= 15):
                    continue
                digico = self._resolve_input_lv1_digico(lv1_idx)
                if digico is not None:
                    slots[int(strip)] = digico
            if any(slots):
                return slots
            if int(layer_id) in self._custom_layer_ids_set.get(s, set()):
                return slots
        return self.get_layer_channels(page_idx, is_custom=is_custom, surface_id=s)

    def infer_link_group_from_digico_overlap(
        self, digicos: List[int],
    ) -> int:
        """Pick the link group whose members best match visible DiGiCo channels."""
        visible = {int(c) for c in digicos if c}
        if not visible:
            return 0
        best_link = 0
        best_count = 0
        for link_n in sorted(self._spill_groups.keys()):
            members = set(self._get_link_digico_members(int(link_n)))
            overlap = len(visible & members)
            if overlap > best_count:
                best_count = overlap
                best_link = int(link_n)
        if best_link <= 0:
            return 0
        n_members = len(self._spill_groups.get(best_link, []))
        need = 1 if n_members <= 2 else 2
        return best_link if best_count >= need else 0

    def infer_link_group_for_digico(self, digico_ch: int) -> int:
        """Return link group (1-16) containing *digico_ch* (1-based), or 0."""
        try:
            ch = int(digico_ch)
        except (TypeError, ValueError):
            return 0
        for link_n, lv1_members in self._spill_groups.items():
            for lv1_idx in lv1_members:
                mapped = self._lv1_to_digico.get(int(lv1_idx))
                if mapped == ch:
                    return int(link_n)
        return 0

    def infer_link_group_for_lv1_index(self, lv1_idx: int) -> int:
        """Return link group (1-16) containing *lv1_idx* (0-based), or 0."""
        try:
            ch = int(lv1_idx)
        except (TypeError, ValueError):
            return 0
        for link_n, members in self._spill_groups.items():
            if ch in members:
                return int(link_n)
        return 0

    def probe_spill_link_group(self, surface_id: int = 2) -> int:
        """Return spilled link group (1-16) from TrackLinkIndex (prop 10).

        Prop 10 pulses on spill enter then returns to 0.  Do not use
        eProp_SelLinkOnDCATouch (1066) — it stays at 1 regardless of which
        link is spilled on this LV1 build.
        """
        return self.read_track_link_index(surface_id)

    def capture_spill_link_group(
        self,
        surface_id: int = 2,
        attempts: int = 12,
        delay_s: float = 0.025,
    ) -> int:
        """Burst-read prop 10 on spill enter; return last non-zero value seen."""
        seen = 0
        for _ in range(max(1, attempts)):
            idx = self.read_track_link_index(surface_id)
            if idx > 0:
                seen = idx
            if delay_s > 0:
                time.sleep(delay_s)
        return seen

    def _read_link_group_index(
        self,
        surface_id: int = 2,
        *,
        allow_type_fallback: bool = True,
    ) -> int:
        """Return active link group (1-16) for spill mapping.

        Live capture: eProp_TrackLinkIndex (10) tracks the spilled link
        (1=Link 1, 2=Link 2, …).  eProp_TrackLinkType (9) stays at the
        configured/default spill link (often 12 → Link 1) and must not
        override TrackLinkIndex while spill is active.
        """
        active = self.probe_spill_link_group(surface_id)
        if active > 0:
            return active
        if not allow_type_fallback:
            return 0
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA query_only = ON")
            row = conn.execute(
                "SELECT value FROM surface_property_int"
                " WHERE surface=? AND property=?",
                (int(surface_id), self.PROP_TRACK_LINK_TYPE),
            ).fetchone()
            conn.close()
            if row is not None:
                return self._sanitize_link_from_type(int(row[0]))
        except Exception:
            pass
        return 0

    @staticmethod
    def _effective_mtime(path: str) -> float:
        """Return newest mtime for an LV1 SQLite file (main + WAL)."""
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return 0.0
        wal_path = path + "-wal"
        try:
            mtime = max(mtime, os.path.getmtime(wal_path))
        except OSError:
            pass
        return mtime

    def _db_change_token(self) -> tuple:
        """Fingerprint main DB + WAL so routing edits are not missed."""
        try:
            main_st = os.stat(self.db_path)
            main_sig = (main_st.st_mtime, main_st.st_size)
        except OSError:
            main_sig = (0.0, 0)
        wal_path = self.db_path + "-wal"
        try:
            wal_st = os.stat(wal_path)
            wal_sig = (wal_st.st_mtime, wal_st.st_size)
        except OSError:
            wal_sig = (0.0, 0)
        return (main_sig, wal_sig)

    def _db_token_changed(self) -> bool:
        return self._db_change_token() != self._last_db_token

    def _note_db_token(self) -> None:
        token = self._db_change_token()
        self._last_db_token = token
        self._last_mtime = max(token[0][0], token[1][0])

    @classmethod
    def _find_paired_emo_name(cls, autosave_path: str) -> str:
        """Match CurrentLV1.dat to the sibling .emo LV1 saved at the same time."""
        p = Path(autosave_path)
        if p.stem.lower() not in _AUTOSAVE_STEMS:
            return ""
        sessions_dir = p.parent
        if not sessions_dir.is_dir():
            return ""
        ref_mtime = cls._effective_mtime(str(p))
        if ref_mtime <= 0:
            return ""

        best_name = ""
        best_key = (_EMO_PAIR_MAX_DELTA_SEC + 1.0, float("inf"))
        for emo in sessions_dir.glob("*.emo"):
            emo_mtime = cls._effective_mtime(str(emo))
            delta = abs(emo_mtime - ref_mtime)
            if delta <= _EMO_PAIR_MAX_DELTA_SEC:
                key = (delta, -emo_mtime)
                if key < best_key:
                    best_key = key
                    best_name = emo.stem
        return best_name

    @classmethod
    def _read_lv1_recent_session(cls) -> str:
        """Return the stem of the most-recently opened LV1 session from the registry.

        LV1 writes HKCU\\SOFTWARE\\Waves Audio\\LV1\\recentFileList (REG_MULTI_SZ)
        with the last-opened .emo path at index 0.  This is the most reliable
        source of the current session name when CurrentLV1.dat is the active file.
        """
        if sys.platform != "win32":
            return ""
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"SOFTWARE\Waves Audio\LV1",
            )
            val, _ = winreg.QueryValueEx(key, "recentFileList")
            winreg.CloseKey(key)
            if val and isinstance(val, (list, tuple)) and val[0]:
                return Path(str(val[0])).stem
        except Exception:
            pass
        return ""

    @classmethod
    def get_emo_path(cls) -> str:
        """Return the full path of the most-recently opened LV1 .emo file, or ''."""
        if sys.platform != "win32":
            return ""
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"SOFTWARE\Waves Audio\LV1",
            )
            val, _ = winreg.QueryValueEx(key, "recentFileList")
            winreg.CloseKey(key)
            if val and isinstance(val, (list, tuple)) and val[0]:
                p = Path(str(val[0]))
                if p.exists():
                    return str(p)
        except Exception:
            pass
        return ""

    @classmethod
    def _read_session_name(cls, conn: sqlite3.Connection, db_path: str,
                           parser: Optional["Lv1SessionParser"] = None) -> str:
        """Best-effort LV1 session name extraction."""
        # For the autosave file (CurrentLV1.dat), the registry recentFileList
        # is the most reliable source — LV1 always writes the last-opened .emo
        # path there, even if the live routes have diverged from the saved file.
        stem = Path(db_path).stem
        if stem.lower() in _AUTOSAVE_STEMS:
            reg_name = cls._read_lv1_recent_session()
            if reg_name:
                return reg_name

        candidate_queries = [
            "SELECT name FROM session LIMIT 1",
            "SELECT session_name FROM session LIMIT 1",
            "SELECT project_name FROM session LIMIT 1",
            "SELECT show_name FROM session LIMIT 1",
            "SELECT value FROM meta WHERE key IN ('session_name', 'session', 'name') LIMIT 1",
            "SELECT value FROM app_meta WHERE key IN ('session_name', 'session', 'name') LIMIT 1",
            "SELECT value FROM app_config WHERE key IN ('session_name', 'session', 'name') LIMIT 1",
            "SELECT value FROM file_properties WHERE name IN "
            "('Session Name', 'Session', 'File Name', 'Filename') LIMIT 1",
        ]
        for query in candidate_queries:
            try:
                row = conn.execute(query).fetchone()
                if row and row[0]:
                    text = str(row[0]).strip()
                    if text:
                        return text
            except Exception:
                continue

        if stem.lower() in _AUTOSAVE_STEMS and parser is not None:
            paired = parser._resolve_autosave_display_name(conn)
            if paired:
                return paired

        paired = cls._find_paired_emo_name(db_path)
        if paired:
            return paired

        if stem.lower() not in _AUTOSAVE_STEMS:
            return stem
        return ""
