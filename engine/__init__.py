"""engine — the capture engine: one shared headed Chrome (raw CDP) that beats Cloudflare Turnstile.

  server.py   the HTTP server holding the browser (ops on a port)
  cdp.py      the CDP client: launch/attach Chrome, flat sessions, raw JSON
  connect.py  reach it, START it if it's down, and turn a page into readable text  <- use this
  cli.py      the `playwrong` command
  client.py   the interactive CLI (goto/click/key/js/tabs/…)
  memcap.py   the systemd memory cap the engine and crawl run under
  mcp_server.py  the MCP stdio front-end

Nothing here needs to be started by hand; connect.ensure() does it.
"""
