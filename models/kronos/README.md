# Vendored Kronos model source

`models/kronos/` holds the model code vendored from
https://github.com/shiyu-coder/Kronos (MIT license, see LICENSE there) —
specifically the `model/` package: `Kronos`, `KronosTokenizer`,
`KronosPredictor`. Pre-trained weights are NOT vendored; they download from
the Hugging Face Hub on first use (`NeoQuasar/Kronos-small`,
`NeoQuasar/Kronos-Tokenizer-base`).

Only `bot/kronos_signal.py` imports from here, lazily. Without this folder
(or without torch installed) the bot runs normally — Kronos simply reports
"unavailable" and stays out of the vote.
