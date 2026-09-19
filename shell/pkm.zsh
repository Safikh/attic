# ===================================================================
# PKM Shell Integration (Thin Aliases)
# All business logic resides in the Python `pkm` CLI.
# ===================================================================

# Fast capture shortcuts
in()      { pkm capture "$@"; }
win()     { pkm capture -v w "$@"; }
inclip()  { pkm capture --clip; }
winclip() { pkm capture -v w --clip; }

# Direct active task addition
act()     { pkm act "$@"; }
wact()    { pkm act -v w "$@"; }

# 2-hour cooldown greeting on interactive new tabs/sessions
pkm_greeting() {
  [[ -o interactive ]] || return 0
  pkm dash --greeting
}

pkm_greeting
