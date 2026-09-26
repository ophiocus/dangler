# myevery — a wrapper's name tag, no code

The myevery bus is a hosted MCP endpoint, so its fleet entry is `url` +
`header_file` and dangler is the client; there is no server to build here.
This folder exists because **every fleet entry carries its skill**: `SKILL.md`
is the name tag dangler installs into the client's skills directory at start,
telling a session *when* to reach for the bus before any schema is loaded.

The service itself lives in its own repository (`ophiocus/myevery`); its API,
the piped transport and the no-storage rule are documented there.
