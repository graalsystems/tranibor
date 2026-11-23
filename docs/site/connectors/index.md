---
title: Connector setup
description: One page per connector, knowledge source, MCP-door client and external agent, the platform's and the plugins', saying what to do at the vendor and in Tranibor to connect it.
---

# Connector setup

Every system Tranibor connects to has a **setup procedure**: what to create at the vendor, which
permissions to give, what to paste in which field, how to test the connection and what its usual
failures mean. This section has one page per procedure, grouped by the kind of system, then one group
per plugin.

**What you will be able to do**

- Find the page of the system you want to connect, before you open the console.
- Read what you need at the vendor, and who must do it.
- Recognise an error you meet and what to do about it.

## What a procedure is

A procedure is a Markdown file with a fixed shape ([ADR 0067](../../adr/0067-a-procedure-per-connector-and-agent-type.md)):
**Before you start**, **At** the vendor, **Permissions**, **In Tranibor**, **Test it** and **When it
fails**, then the fields of the form, the hosts the platform must reach and the vendor's own
documentation. The platform ships one for each connector type, knowledge source kind, MCP-door client
and external agent kind. A plugin ships one for each server it brings: its procedure applies to
`mcp/<plugin>` or `mcp/<server>`.

## Where it shows in the console

The console shows the same text, with your deployment's own addresses filled in:

- on `/admin/connectors`, with **How to set this up** beside the form that adds a connector, and on
  the settings of a connector that exists. The connector of a plugin's server shows the plugin's own
  procedure; without one, the generic MCP server procedure;
- on `/library/plugins`, where each procedure an installed plugin brought opens with a click;
- beside the forms that add a knowledge source or an external agent.

The first time you meet a procedure it opens by itself; after that it stays one click away.

## How these pages are made

The pages below are **generated** when the site is built, from the procedures that ship with the
platform and from those of the plugins published beside it. They are never written by hand, so they
cannot drift from what the console shows. Here, the addresses are written `<api_url>`, `<public_url>`
and `<org>`: the console writes your deployment's. The same procedures are also in the repository, as
[one Markdown file each](../../procedures/README.md).

A deployment can add its own notes to a procedure, or a procedure for a type of its own: those appear in
its console only, never here.

## Go further

- [Connectors](../use/connectors.md): where each kind is set, and how to tell that it works.
- [Plugins](../build/plugins.md): what a plugin ships, its procedures included.
- [Each connector has a page on the documentation site](../../adr/0215-each-connector-has-a-page-on-the-documentation-site.md),
  the decision behind this section.
