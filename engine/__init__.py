"""engine — the capture engine: one shared headed Chrome (nodriver) that beats Cloudflare Turnstile.

  server.py   the HTTP server holding the browser (ops on a port)
  connect.py  reach it, START it if it's down, and turn a page into readable text  <- use this
  cli.py      the `playwrong` command
  client.py   the interactive CLI (goto/click/key/js/tabs/…)
  solve.py    standalone Turnstile solve

Nothing here needs to be started by hand; connect.ensure() does it.
"""
