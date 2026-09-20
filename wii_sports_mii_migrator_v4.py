#!/usr/bin/env python3
"""
Wii Sports Mii Save Migrator
============================

Patches an existing Wii Sports RPSports.dat so its Mii-linked player records
match the FINAL RFL_DB.dat that you actually want to use.

Only two inputs are required:

    1. RPSports.dat
       The Wii Sports save containing the progress you want to keep.

    2. RFL_DB.dat
       The finalized Mii database that Wii Sports must conform to.

The source/original RFL_DB.dat is NOT needed.

How it works
------------
* Validates the supplied RFL_DB.dat CRC.
* Validates the supplied RPSports.dat checksum.
* Reads every populated Mii in the final RFL_DB.dat.
* Finds active Wii Sports player records by their embedded 4-byte Mii ID.
* Matches each active player to the same Mii ID in RFL_DB.dat.
* Moves the COMPLETE Wii Sports player record to that Mii's current RFL slot.
* Changes only the copied record's embedded identity bytes to match RFL_DB.dat.
* Makes target Mii slots with no matching source Wii Sports progress empty.
* Clears active source slots that were vacated.
* Recalculates and validates the Wii Sports checksum.
* Never modifies the files supplied by the user.

This reproduces the migration method proven on real Wii hardware.

No third-party Python packages are required.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ============================================================================
# Paths
# ============================================================================

SCRIPT_DIR = Path(__file__).resolve().parent


# ============================================================================
# RFL_DB.dat structure
# ============================================================================

RFL_SIGNATURE = b"RNOD"

RFL_MII_BASE = 0x0004
RFL_MII_SIZE = 0x004A
RFL_MII_COUNT = 100

RFL_NAME_OFFSET = 0x02
RFL_NAME_BYTES = 20
RFL_MII_ID_OFFSET = 0x18
RFL_SYSTEM_ID_OFFSET = 0x1C

# CRC-16/CCITT, polynomial 0x1021, initial value 0.
RFL_CRC_OFFSET = 0x1F1DE


# ============================================================================
# Wii Sports RPSports.dat structure
# ============================================================================

RPSPORTS_EXPECTED_SIZE = 0x20000

RPSPORTS_PLAYER_BASE = 0x005F
RPSPORTS_PLAYER_STRIDE = 0x0455
RPSPORTS_PLAYER_COUNT = 100

# Relative offsets inside each Wii Sports player record.
RPSPORTS_STATUS_OFFSET = 0x00
RPSPORTS_MII_ID_OFFSET = 0x01
RPSPORTS_SYSTEM_ID_OFFSET = 0x05

# Four-byte Wii Sports checksum.
RPSPORTS_CHECKSUM_OFFSET = 0x1B190
RPSPORTS_CHECKSUM_SIZE = 4


# ============================================================================
# Console helpers
# ============================================================================

class C:
    ENABLED = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    RESET = "\033[0m"

    @classmethod
    def wrap(cls, text: str, code: str) -> str:
        if not cls.ENABLED:
            return text
        return f"{code}{text}{cls.RESET}"


def heading(text: str) -> None:
    print()
    print(C.wrap("=" * 78, C.CYAN))
    print(C.wrap(text, C.BOLD + C.CYAN))
    print(C.wrap("=" * 78, C.CYAN))


def info(text: str) -> None:
    print(f"{C.wrap('[i]', C.CYAN)} {text}")


def ok(text: str) -> None:
    print(f"{C.wrap('[+]', C.GREEN)} {text}")


def warn(text: str) -> None:
    print(f"{C.wrap('[!]', C.YELLOW)} {text}")


def fail(text: str) -> None:
    print(f"{C.wrap('[X]', C.RED)} {text}")


def yes_no(prompt: str, default: bool = False) -> bool:
    suffix = " [Y/n]: " if default else " [y/N]: "
    while True:
        answer = input(prompt + suffix).strip().lower()

        if not answer:
            return default

        if answer in {"y", "yes"}:
            return True

        if answer in {"n", "no"}:
            return False

        print("Please enter y or n.")


def ask_file(label: str) -> Path:
    while True:
        raw = input(f"{label}: ").strip().strip('"').strip("'")
        path = Path(raw).expanduser()

        if path.is_file():
            return path.resolve()

        fail(f"File not found: {path}")


# ============================================================================
# Data structures
# ============================================================================

@dataclass(frozen=True)
class Mii:
    slot: int
    name: str
    mii_id: bytes
    system_id: bytes

    @property
    def mii_id_hex(self) -> str:
        return self.mii_id.hex().upper()

    @property
    def system_id_hex(self) -> str:
        return self.system_id.hex().upper()


@dataclass(frozen=True)
class SportsPlayer:
    slot: int
    status: int
    mii_id: bytes
    system_id: bytes
    raw: bytes

    @property
    def active(self) -> bool:
        return self.mii_id != b"\x00\x00\x00\x00"

    @property
    def mii_id_hex(self) -> str:
        return self.mii_id.hex().upper()

    @property
    def system_id_hex(self) -> str:
        return self.system_id.hex().upper()


@dataclass(frozen=True)
class PlayerMove:
    player: SportsPlayer
    target_mii: Mii

    @property
    def source_slot(self) -> int:
        return self.player.slot

    @property
    def target_slot(self) -> int:
        return self.target_mii.slot


# ============================================================================
# Generic helpers
# ============================================================================

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()

    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)

    return h.hexdigest()


def decode_utf16be_name(raw: bytes) -> str:
    return raw.decode("utf-16-be", errors="replace").split("\x00", 1)[0]


# ============================================================================
# RFL_DB.dat parsing / validation
# ============================================================================

def crc16_ccitt(data: bytes) -> int:
    crc = 0

    for byte in data:
        crc ^= byte << 8

        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF

    return crc


def validate_rfl(data: bytes) -> Tuple[int, int]:
    if len(data) < RFL_CRC_OFFSET + 2:
        raise ValueError(
            f"RFL_DB.dat is too small ({len(data)} bytes)."
        )

    if data[:4] != RFL_SIGNATURE:
        raise ValueError(
            "RFL_DB.dat does not begin with the expected RNOD signature."
        )

    stored = int.from_bytes(
        data[RFL_CRC_OFFSET:RFL_CRC_OFFSET + 2],
        "big",
    )
    calculated = crc16_ccitt(data[:RFL_CRC_OFFSET])

    return stored, calculated


def parse_rfl(data: bytes) -> List[Mii]:
    records: List[Mii] = []

    for slot in range(RFL_MII_COUNT):
        start = RFL_MII_BASE + slot * RFL_MII_SIZE
        raw = data[start:start + RFL_MII_SIZE]

        if len(raw) != RFL_MII_SIZE:
            raise ValueError(
                f"RFL_DB.dat ended inside Mii slot {slot}."
            )

        if not any(raw):
            continue

        records.append(
            Mii(
                slot=slot,
                name=decode_utf16be_name(
                    raw[
                        RFL_NAME_OFFSET:
                        RFL_NAME_OFFSET + RFL_NAME_BYTES
                    ]
                ),
                mii_id=bytes(
                    raw[
                        RFL_MII_ID_OFFSET:
                        RFL_MII_ID_OFFSET + 4
                    ]
                ),
                system_id=bytes(
                    raw[
                        RFL_SYSTEM_ID_OFFSET:
                        RFL_SYSTEM_ID_OFFSET + 4
                    ]
                ),
            )
        )

    return records


def validate_unique_rfl_ids(miis: List[Mii]) -> None:
    seen: Dict[bytes, Mii] = {}

    for mii in miis:
        previous = seen.get(mii.mii_id)

        if previous is not None:
            raise ValueError(
                "RFL_DB.dat contains duplicate Mii ID "
                f"{mii.mii_id_hex} in slots {previous.slot} and {mii.slot}. "
                "The migrator will not guess which Mii should receive progress."
            )

        seen[mii.mii_id] = mii


# ============================================================================
# RPSports.dat parsing / validation
# ============================================================================

def player_slot_offset(slot: int) -> int:
    return RPSPORTS_PLAYER_BASE + slot * RPSPORTS_PLAYER_STRIDE


def player_slot_length(slot: int) -> int:
    """
    Returns the amount of the player record that lies before the Wii Sports
    checksum.

    Slots 0-98 contain the complete 0x455-byte record. The final record ends
    at the checksum boundary and is therefore slightly shorter.
    """
    start = player_slot_offset(slot)

    if start >= RPSPORTS_CHECKSUM_OFFSET:
        return 0

    return min(
        RPSPORTS_PLAYER_STRIDE,
        RPSPORTS_CHECKSUM_OFFSET - start,
    )


def calculate_rpsports_checksum(data: bytes) -> bytes:
    """
    Checksum at 0x1B190.

    Treat bytes [0, 0x1B190) as big-endian 16-bit words.

      upper 16 bits = sum(words) mod 0x10000
      lower 16 bits = sum(word XOR 0xFFFF) mod 0x10000
    """
    region = data[:RPSPORTS_CHECKSUM_OFFSET]

    if len(region) % 2:
        raise ValueError("Wii Sports checksum region has an odd length.")

    total = 0
    inverse_total = 0

    for offset in range(0, len(region), 2):
        word = int.from_bytes(region[offset:offset + 2], "big")

        total = (total + word) & 0xFFFF
        inverse_total = (
            inverse_total + (word ^ 0xFFFF)
        ) & 0xFFFF

    return (
        total.to_bytes(2, "big")
        + inverse_total.to_bytes(2, "big")
    )


def validate_rpsports(data: bytes) -> Tuple[bytes, bytes]:
    if len(data) != RPSPORTS_EXPECTED_SIZE:
        raise ValueError(
            "RPSports.dat has an unexpected size: "
            f"{len(data)} bytes; expected "
            f"{RPSPORTS_EXPECTED_SIZE} bytes."
        )

    stored = data[
        RPSPORTS_CHECKSUM_OFFSET:
        RPSPORTS_CHECKSUM_OFFSET + RPSPORTS_CHECKSUM_SIZE
    ]
    calculated = calculate_rpsports_checksum(data)

    return stored, calculated


def parse_sports_players(data: bytes) -> List[SportsPlayer]:
    players: List[SportsPlayer] = []

    for slot in range(RPSPORTS_PLAYER_COUNT):
        start = player_slot_offset(slot)
        length = player_slot_length(slot)

        if length < RPSPORTS_SYSTEM_ID_OFFSET + 4:
            continue

        raw = bytes(data[start:start + length])

        players.append(
            SportsPlayer(
                slot=slot,
                status=raw[RPSPORTS_STATUS_OFFSET],
                mii_id=bytes(
                    raw[
                        RPSPORTS_MII_ID_OFFSET:
                        RPSPORTS_MII_ID_OFFSET + 4
                    ]
                ),
                system_id=bytes(
                    raw[
                        RPSPORTS_SYSTEM_ID_OFFSET:
                        RPSPORTS_SYSTEM_ID_OFFSET + 4
                    ]
                ),
                raw=raw,
            )
        )

    return players


def active_sports_players(data: bytes) -> List[SportsPlayer]:
    return [
        player
        for player in parse_sports_players(data)
        if player.active
    ]


def validate_unique_sports_ids(players: List[SportsPlayer]) -> None:
    seen: Dict[bytes, SportsPlayer] = {}

    for player in players:
        previous = seen.get(player.mii_id)

        if previous is not None:
            raise ValueError(
                "RPSports.dat contains duplicate active Mii ID "
                f"{player.mii_id_hex} in player slots "
                f"{previous.slot} and {player.slot}. "
                "The migrator will not guess."
            )

        seen[player.mii_id] = player


# ============================================================================
# Migration planning
# ============================================================================

def build_plan(
    sports_data: bytes,
    target_miis: List[Mii],
) -> List[PlayerMove]:

    target_by_id = {
        mii.mii_id: mii
        for mii in target_miis
    }

    players = active_sports_players(sports_data)

    if not players:
        raise ValueError(
            "No active Mii-linked Wii Sports player records were found."
        )

    validate_unique_sports_ids(players)

    moves: List[PlayerMove] = []

    for player in players:
        target_mii = target_by_id.get(player.mii_id)

        if target_mii is None:
            raise ValueError(
                "The Wii Sports save contains an active player with Mii ID "
                f"{player.mii_id_hex} in slot {player.slot}, but that Mii ID "
                "does not exist in the supplied RFL_DB.dat.\n"
                "Nothing was changed. Add/fix that Mii in RFL_DB.dat first."
            )

        moves.append(
            PlayerMove(
                player=player,
                target_mii=target_mii,
            )
        )

    # A target slot cannot receive more than one player.
    destination_slots: Dict[int, PlayerMove] = {}

    for move in moves:
        previous = destination_slots.get(move.target_slot)

        if previous is not None:
            raise ValueError(
                f"Two Wii Sports players would be written to target slot "
                f"{move.target_slot}. Refusing to continue."
            )

        destination_slots[move.target_slot] = move

    moves.sort(key=lambda item: item.source_slot)

    return moves


def print_target_miis(miis: List[Mii]) -> None:
    heading("Final RFL_DB.dat Mii layout")

    print(
        f"{'Slot':>4}  {'Name':<22}  "
        f"{'Mii ID':<8}  {'System ID':<8}"
    )
    print("-" * 62)

    for mii in sorted(miis, key=lambda item: item.slot):
        print(
            f"{mii.slot:>4}  "
            f"{mii.name[:22]:<22}  "
            f"{mii.mii_id_hex:<8}  "
            f"{mii.system_id_hex:<8}"
        )


def plan_lines(moves: List[PlayerMove]) -> List[str]:
    lines: List[str] = []

    for move in moves:
        player = move.player
        mii = move.target_mii

        changes = []

        if player.slot != mii.slot:
            changes.append(f"slot {player.slot} -> {mii.slot}")
        else:
            changes.append(f"slot {player.slot} unchanged")

        if player.system_id != mii.system_id:
            changes.append(
                f"System ID {player.system_id_hex} -> {mii.system_id_hex}"
            )
        else:
            changes.append(
                f"System ID {mii.system_id_hex} already matches"
            )

        lines.append(
            f"{mii.name!r:24} "
            f"Mii ID {mii.mii_id_hex} | "
            + " | ".join(changes)
        )

    return lines


# ============================================================================
# Patching
# ============================================================================

def patch_rpsports(
    original: bytes,
    target_miis: List[Mii],
    moves: List[PlayerMove],
) -> Tuple[bytes, bytes, bytes, List[int]]:
    """
    Rebuild only the Wii Sports Mii-linked player placement needed to match the
    supplied RFL_DB.dat.

    Proven behavior:
      * Work from immutable snapshots of the original active records.
      * Empty every populated target RFL slot first.
      * Empty every active source slot first.
      * Write each complete saved player record into its target Mii slot.
      * Change only the copied Mii ID/System ID identity bytes.
      * Recalculate the Wii Sports checksum.

    Emptying target Mii slots that have no source Wii Sports progress avoids
    leaving a stale player record assigned to the wrong target Mii.
    """
    out = bytearray(original)

    # Snapshot active source records BEFORE any writes.
    source_snapshots: Dict[int, bytes] = {
        move.source_slot: bytes(move.player.raw)
        for move in moves
    }

    target_populated_slots = {
        mii.slot
        for mii in target_miis
    }

    active_source_slots = {
        move.source_slot
        for move in moves
    }

    slots_to_clear = sorted(
        target_populated_slots | active_source_slots
    )

    # Clear only Wii Sports player-record bytes. Never touch checksum bytes.
    for slot in slots_to_clear:
        start = player_slot_offset(slot)
        length = player_slot_length(slot)

        if length <= 0:
            raise ValueError(
                f"Cannot safely address Wii Sports player slot {slot}."
            )

        out[start:start + length] = b"\x00" * length

    # Reinsert complete immutable records at their target RFL positions.
    for move in moves:
        source_record = source_snapshots[move.source_slot]

        target_start = player_slot_offset(move.target_slot)
        target_length = player_slot_length(move.target_slot)

        copy_length = min(
            len(source_record),
            target_length,
        )

        out[
            target_start:
            target_start + copy_length
        ] = source_record[:copy_length]

        # Identity bytes inside the copied record.
        mii_id_offset = (
            target_start + RPSPORTS_MII_ID_OFFSET
        )
        system_id_offset = (
            target_start + RPSPORTS_SYSTEM_ID_OFFSET
        )

        out[
            mii_id_offset:
            mii_id_offset + 4
        ] = move.target_mii.mii_id

        out[
            system_id_offset:
            system_id_offset + 4
        ] = move.target_mii.system_id

    old_checksum = bytes(
        original[
            RPSPORTS_CHECKSUM_OFFSET:
            RPSPORTS_CHECKSUM_OFFSET + RPSPORTS_CHECKSUM_SIZE
        ]
    )

    new_checksum = calculate_rpsports_checksum(bytes(out))

    out[
        RPSPORTS_CHECKSUM_OFFSET:
        RPSPORTS_CHECKSUM_OFFSET + RPSPORTS_CHECKSUM_SIZE
    ] = new_checksum

    patched = bytes(out)

    # Final checksum validation.
    stored, calculated = validate_rpsports(patched)

    if stored != calculated:
        raise RuntimeError(
            "Internal validation failed: generated Wii Sports checksum "
            "does not verify."
        )

    # Verify every moved player identity at its final target slot.
    final_players = {
        player.slot: player
        for player in parse_sports_players(patched)
    }

    for move in moves:
        final = final_players.get(move.target_slot)

        if final is None:
            raise RuntimeError(
                f"Internal validation failed for target slot "
                f"{move.target_slot}."
            )

        if final.mii_id != move.target_mii.mii_id:
            raise RuntimeError(
                f"Internal validation failed for {move.target_mii.name!r}: "
                "Mii ID does not match after patch."
            )

        if final.system_id != move.target_mii.system_id:
            raise RuntimeError(
                f"Internal validation failed for {move.target_mii.name!r}: "
                "System ID does not match after patch."
            )

    return patched, old_checksum, new_checksum, slots_to_clear


# ============================================================================
# Workspace
# ============================================================================

def unique_run_directory() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    candidate = (
        SCRIPT_DIR /
        f"wii_sports_mii_patch_{timestamp}"
    )

    number = 1

    while candidate.exists():
        candidate = (
            SCRIPT_DIR /
            f"wii_sports_mii_patch_{timestamp}_{number:02d}"
        )
        number += 1

    return candidate


def create_workspace() -> Tuple[Path, Path, Path, Path]:
    run_dir = unique_run_directory()
    backup_dir = run_dir / "backup"
    processing_dir = run_dir / "processing"
    output_dir = run_dir / "output"

    backup_dir.mkdir(parents=True)
    processing_dir.mkdir()
    output_dir.mkdir()

    return (
        run_dir,
        backup_dir,
        processing_dir,
        output_dir,
    )


def prepare_workspace(
    rpsports_path: Path,
    rfl_path: Path,
) -> Tuple[
    Path,
    Path,
    Path,
    Path,
    Path,
    Path,
]:
    (
        run_dir,
        backup_dir,
        processing_dir,
        output_dir,
    ) = create_workspace()

    backup_sports = backup_dir / "RPSports.dat"
    backup_rfl = backup_dir / "RFL_DB.dat"

    processing_sports = processing_dir / "RPSports.dat"
    processing_rfl = processing_dir / "RFL_DB.dat"

    shutil.copy2(rpsports_path, backup_sports)
    shutil.copy2(rfl_path, backup_rfl)

    shutil.copy2(rpsports_path, processing_sports)
    shutil.copy2(rfl_path, processing_rfl)

    return (
        run_dir,
        backup_dir,
        processing_dir,
        output_dir,
        processing_sports,
        processing_rfl,
    )


# ============================================================================
# Report
# ============================================================================

def write_report(
    report_path: Path,
    input_sports_hash: str,
    input_rfl_hash: str,
    output_hash: str,
    target_miis: List[Mii],
    moves: List[PlayerMove],
    old_checksum: bytes,
    new_checksum: bytes,
    slots_cleared: List[int],
) -> None:

    lines: List[str] = []

    lines.append("Wii Sports Mii Save Migrator - Patch Report")
    lines.append("=" * 78)
    lines.append(
        f"Created: {datetime.now().isoformat(timespec='seconds')}"
    )
    lines.append("")

    lines.append("INPUTS")
    lines.append("-" * 78)
    lines.append(
        f"RPSports.dat SHA-256: {input_sports_hash}"
    )
    lines.append(
        f"RFL_DB.dat   SHA-256: {input_rfl_hash}"
    )
    lines.append("")

    lines.append("FINAL RFL MII LAYOUT")
    lines.append("-" * 78)

    for mii in sorted(target_miis, key=lambda item: item.slot):
        lines.append(
            f"slot {mii.slot:2}: "
            f"{mii.name!r:24} "
            f"Mii ID={mii.mii_id_hex} "
            f"System ID={mii.system_id_hex}"
        )

    lines.append("")
    lines.append("WII SPORTS PLAYER MIGRATION")
    lines.append("-" * 78)
    lines.extend(plan_lines(moves))

    lines.append("")
    lines.append("PATCH DETAILS")
    lines.append("-" * 78)
    lines.append(
        "Player slots cleared before rebuilding: "
        + ", ".join(str(slot) for slot in slots_cleared)
    )
    lines.append(
        f"Wii Sports checksum: "
        f"{old_checksum.hex().upper()} -> "
        f"{new_checksum.hex().upper()}"
    )

    lines.append("")
    lines.append("OUTPUT")
    lines.append("-" * 78)
    lines.append(
        f"RPSports.dat SHA-256: {output_hash}"
    )

    lines.append("")
    lines.append("SAFETY")
    lines.append("-" * 78)
    lines.append(
        "backup/ contains untouched copies of both supplied input files."
    )
    lines.append(
        "processing/ contains the working copies used for analysis."
    )
    lines.append(
        "output/RPSports.dat is the patched file intended for installation."
    )
    lines.append(
        "The original paths supplied to the script were never modified."
    )

    report_path.write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


# ============================================================================
# Arguments / main
# ============================================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Patch a Wii Sports RPSports.dat to match a finalized "
            "RFL_DB.dat. No source RFL_DB.dat is required."
        )
    )

    parser.add_argument(
        "--rpsports",
        type=Path,
        help="RPSports.dat containing the progress to keep.",
    )

    parser.add_argument(
        "--rfl",
        type=Path,
        help="Final RFL_DB.dat that Wii Sports must match.",
    )

    parser.add_argument(
        "--yes",
        action="store_true",
        help="Write output without interactive confirmation.",
    )

    return parser


def resolve_inputs(
    args: argparse.Namespace,
) -> Tuple[Path, Path]:

    if args.rpsports is None:
        rpsports_path = ask_file(
            "Path to RPSports.dat containing the progress to keep"
        )
    else:
        rpsports_path = args.rpsports.expanduser().resolve()

    if args.rfl is None:
        rfl_path = ask_file(
            "Path to the FINAL RFL_DB.dat to use"
        )
    else:
        rfl_path = args.rfl.expanduser().resolve()

    if not rpsports_path.is_file():
        raise FileNotFoundError(
            f"RPSports.dat not found: {rpsports_path}"
        )

    if not rfl_path.is_file():
        raise FileNotFoundError(
            f"RFL_DB.dat not found: {rfl_path}"
        )

    return rpsports_path, rfl_path


def main() -> int:
    args = build_parser().parse_args()

    heading("Wii Sports Mii Save Migrator")

    print(
        "This version needs only:\n"
        "  1. the Wii Sports RPSports.dat containing the progress to keep\n"
        "  2. the FINAL RFL_DB.dat that the save must match\n"
    )

    info(
        "All generated folders are created beside this Python script:"
    )
    info(str(SCRIPT_DIR))

    try:
        rpsports_path, rfl_path = resolve_inputs(args)

        heading("Creating protected workspace")

        (
            run_dir,
            backup_dir,
            processing_dir,
            output_dir,
            processing_sports,
            processing_rfl,
        ) = prepare_workspace(
            rpsports_path,
            rfl_path,
        )

        info(f"Run folder : {run_dir}")
        info(f"Backup     : {backup_dir}")
        info(f"Processing : {processing_dir}")
        info(f"Output     : {output_dir}")

        ok("Both original input files were copied into backup/.")
        ok("Only processing copies will be read/used from this point.")

        sports_data = processing_sports.read_bytes()
        rfl_data = processing_rfl.read_bytes()

        heading("Validating files")

        rfl_stored_crc, rfl_calculated_crc = validate_rfl(
            rfl_data
        )

        info(
            f"RFL_DB.dat CRC: stored {rfl_stored_crc:04X}, "
            f"calculated {rfl_calculated_crc:04X}"
        )

        if rfl_stored_crc != rfl_calculated_crc:
            raise ValueError(
                "RFL_DB.dat CRC is invalid. "
                "Repair the Mii database before using it here."
            )

        sports_stored_ck, sports_calculated_ck = validate_rpsports(
            sports_data
        )

        info(
            "RPSports.dat checksum: stored "
            f"{sports_stored_ck.hex().upper()}, calculated "
            f"{sports_calculated_ck.hex().upper()}"
        )

        if sports_stored_ck != sports_calculated_ck:
            raise ValueError(
                "RPSports.dat checksum is invalid. "
                "The migrator will not patch a damaged source save."
            )

        ok("Both input files validated successfully.")

        target_miis = parse_rfl(rfl_data)

        if not target_miis:
            raise ValueError(
                "The supplied RFL_DB.dat contains no populated Miis."
            )

        validate_unique_rfl_ids(target_miis)

        print_target_miis(target_miis)

        moves = build_plan(
            sports_data,
            target_miis,
        )

        heading("Proposed Wii Sports changes")

        for line in plan_lines(moves):
            print(line)

        target_ids_with_progress = {
            move.target_mii.mii_id
            for move in moves
        }

        target_without_progress = [
            mii
            for mii in target_miis
            if mii.mii_id not in target_ids_with_progress
        ]

        if target_without_progress:
            print()
            info(
                "These target Miis have no matching active progress in "
                "the supplied RPSports.dat and their Wii Sports player "
                "slots will be empty:"
            )

            for mii in target_without_progress:
                print(
                    f"  slot {mii.slot:2}: "
                    f"{mii.name!r} "
                    f"({mii.mii_id_hex})"
                )

        patched, old_ck, new_ck, slots_cleared = patch_rpsports(
            sports_data,
            target_miis,
            moves,
        )

        print()
        info(
            f"Wii Sports checksum will change "
            f"{old_ck.hex().upper()} -> {new_ck.hex().upper()}"
        )

        if not args.yes:
            print()

            if not yes_no(
                "Write the validated patched RPSports.dat?",
                default=True,
            ):
                warn(
                    "Cancelled. The backup/ and processing/ copies "
                    "were preserved; no patched output was written."
                )
                return 1

        output_sports = output_dir / "RPSports.dat"
        output_report = output_dir / "patch_report.txt"

        output_sports.write_bytes(patched)

        input_sports_hash = sha256_file(
            backup_dir / "RPSports.dat"
        )
        input_rfl_hash = sha256_file(
            backup_dir / "RFL_DB.dat"
        )
        output_hash = sha256_bytes(patched)

        write_report(
            report_path=output_report,
            input_sports_hash=input_sports_hash,
            input_rfl_hash=input_rfl_hash,
            output_hash=output_hash,
            target_miis=target_miis,
            moves=moves,
            old_checksum=old_ck,
            new_checksum=new_ck,
            slots_cleared=slots_cleared,
        )

        heading("Completed")

        ok(f"Patched save: {output_sports}")
        ok(f"Report:       {output_report}")

        print()
        print(
            f"Checksum: {old_ck.hex().upper()} "
            f"-> {new_ck.hex().upper()}"
        )
        print(f"SHA-256: {output_hash}")

        print()
        ok("The supplied RPSports.dat was not modified.")
        ok("The supplied RFL_DB.dat was not modified.")

        return 0

    except KeyboardInterrupt:
        print()
        warn("Cancelled by user.")
        return 130

    except Exception as exc:
        print()
        fail(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
