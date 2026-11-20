Schema v1 is defined by the SQLAlchemy models and created at startup via Base.metadata.create_all. For this local small application no destructive migration is run. Back up named db-data before modifying schemas.


## v2 additions (exclusion trials)

`exclusion_trials` stores a frozen trial basis. A unique
`(study_id, basis_checksum)` index makes an interrupted retry return the
existing row instead of creating a second trial. The checksum covers the
study id, the algorithm version, the sorted must-keep keys and every row's
observation id plus values, so any edit, delete or re-add invalidates a
previously previewed plan.

`create_all` adds the new table on startup; the existing MySQL columns are
unchanged, so this is additive only. The verification record lives in
`TESTING.md` at the repository root.
