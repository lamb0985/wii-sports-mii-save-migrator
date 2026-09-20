Wii Sports Mii Save Migrator
============================

Patches an existing Wii Sports RPSports.dat so its Mii-linked player records
match the FINAL RFL_DB.dat that you actually want to use.

Only two inputs are required:

    1. RPSports.dat
       The Wii Sports save containing the progress you want to keep.

    2. RFL_DB.dat
       The finalized Mii database that Wii Sports must conform to.

SAFETY
------------------------------------------------------------------------------
The script will create subdirectories in the same directory the script was run:

backup/ contains untouched copies of both supplied input files.

processing/ contains the working copies used for analysis.

output/RPSports.dat is the patched file intended for installation.

The original paths supplied to the script are never modified.

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
