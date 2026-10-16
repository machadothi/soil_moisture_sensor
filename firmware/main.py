# Runs at every boot. Everything is in app.py; see README.md.
# To stop it: Ctrl+C in `mpremote` (or any serial terminal) gives the REPL.
# While the board deep-sleeps it can't hear Ctrl+C: press the reset (EN)
# button, and it stays awake for a while after a reset.
#
# On a crash, sleep and try again rather than stop with the battery draining.
import sys

import machine

try:
    import app
    app.run()
except Exception as e:   # not KeyboardInterrupt: Ctrl+C still reaches the REPL
    sys.print_exception(e)
    machine.deepsleep(60000)
