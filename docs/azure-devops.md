# Azure DevOps

SAFO runs on Azure DevOps as well as on GitHub. A board file with `project.platform: azure-devops` describes one Azure
DevOps project: its work items are the cards, its Azure Repos pull requests must link a work item, and the states,
fields, area paths, iterations and shared queries the file names are kept in line with the project's process. The same
commands do the work, with the same exit codes: `validate`, `bootstrap`, `audit`, `reconcile`, `sync`, `status` and
`agents-status`. The local commands (`usage`, `route`, `hooks` and the rest) do not read a board and work as before.

A complete file is [examples/azure-devops.yaml](../examples/azure-devops.yaml).

## How board.yaml maps onto Azure Boards

| board.yaml | Azure DevOps |
|---|---|
| `project.owner` | The organization: `https://dev.azure.com/<owner>` |
| `project.title` | The project's name |
| `azure_devops.work_item_types` | The work item types that are cards (`User Story`, `Bug`, `Product Backlog Item`, ...) |
| `rules.status_field` (a `single_select`) | The work item **State**. Each option is a state of every listed type; its colour is the state's colour |
| `rules.area_field` (a `single_select`) | The **Area Path**. Each option is an area directly under the project (`Platform\Docs`) |
| The one `iteration` field | The **Iteration Path**: iterations `<title_prefix> 1..count` under the project, with start and finish dates, selected by the team |
| Any other `single_select` | A custom picklist field, `Custom.<Name>` (`Agent` is `Custom.Agent`, `Done on` is `Custom.DoneOn`) |
| `date`, `text`, `number` | A custom date-time, string or double field |
| `views` | Shared queries in `Shared Queries/<queries_folder>`. `filter` is a WIQL condition (`[System.State] = 'Blocked'`); `layout` is not represented |
| `rules.done_date_field` | Filled from the work item's Closed Date when it reaches a Completed state |
| `rules.tracking` | Pull requests with no linked work item, and open work items with no Kind, Epic or Effort |
| `rules.threat_model.label` | A **tag**; the work item's description must hold a `threat-model:` line |
| `rules.gate_check` | A blocking, enabled **build validation** (by its display name) or **status** policy on each repository's default branch |
| `status --post` | A new page `<status_page>/<YYYY-MM-DD>` in the project wiki |

A work item is always on its board, so there is no "add to the board" step. Done is read from the state's category:
a state in the Completed category is done, and a work item in a Removed state is never read or changed.

## The azure_devops block

| Key | Required | Meaning |
|---|---|---|
| `work_item_types` | yes | 1 to 16 work item type names |
| `team` | no | The team whose iterations the iteration field selects. Default: `<project> Team` |
| `process` | no | The inherited process the project must use; `audit` reports another one |
| `default_area`, `area_rules` | no | The Area a work item with the root Area Path gets: the first `{match, area}` whose `match` occurs (case-insensitive) in the title or a tag, else `default_area`. On Azure DevOps a work item is not tied to a repository, so these are here and not under `repositories` |
| `queries_folder` | no | The shared query folder the views live in. Default: `SAFO` |
| `status_page` | no | The wiki path `status --post` writes under. Default: `SAFO Status` |
| `state_categories` | no | `{state: category}` for a state bootstrap creates. Default: the done states Completed, the opened and reopened states Proposed, the rest InProgress |
| `field_refs` | no | `{field: Reference.Name}` to use an existing field (for example `Effort: Microsoft.VSTS.Scheduling.Effort`) instead of `Custom.<Name>` |
| `closed_days` | no | `reconcile` and `audit` also read work items completed in the last N days (default 30), to fill the Done-on date |

Repositories are `{name}` (a repository of the project) or `{owner, name}` (`owner` is another project of the same
organization). `default_area`, `area_rules` and `method` are refused under a repository. An Azure DevOps board has one
process method, `agile`, and at most one iteration field.

## What each command does

* **bootstrap** only adds: missing states, organization fields (a picklist for a `single_select`), the fields on each
  listed work item type, areas, iterations, the team's iteration selection, the query folder and the queries. It never
  changes a state's category, a picklist's items, an iteration's dates or a query's WIQL: `audit` reports those and a
  person changes them. States and fields can only be added to an **inherited** process (Organization settings > Process
  > Create inherited process, then move the project to it); on a system process bootstrap says so and still makes the
  areas, iterations and queries. A type the inherited process still takes unchanged from its parent is derived first,
  which is what the web UI does on the first change. A new field is not placed on the work item form; drag it onto the
  layout once if people should see it there.
* **audit** compares the process and the project with the file, then every open work item (and those completed within
  `closed_days`) with the rules, the pull requests with the no-hidden-work rule, and the branch policies with the gate.
  Exit 0 clean, 1 drift, 2 cannot tell.
* **reconcile** fills what a work item lacks: the Area Path when it is the project's root, each `new_item_defaults` field
  that is blank, and the Done-on date of a completed item. A value someone set is never overwritten, and the State is
  never written. Every write is one JSON Patch whose first operation tests the revision the decision was read from, so a
  work item someone changed in the meantime is refused (FAILED) and left alone. After an unknown outcome nothing more is
  sent. Pull requests with no linked work item and work items with no Kind, Epic or Effort are listed (`HUMAN`).
* **sync** brings one work item in line: `--work-item N`, or a service hook payload with `--event-path FILE` (or
  `$SAFO_EVENT_PATH`) for `workitem.*` and `git.pullrequest.*` events. The payload is only a pointer: the work item or
  pull request is read again. A work item that was moved out of a done state has its Done-on date cleared.
* **status** `--post` groups and counts the work items in code and writes them to a new wiki page; `--print` prints it,
  `--body-file` posts your own text. An existing page is never overwritten. The project needs a project wiki.
* **agents-status** reads the Agent field of the open work items, then the same local sections as on GitHub.

## Credentials

1. `SAFO_ADO_TOKEN`: a personal access token, sent as HTTP Basic. In Azure Pipelines pass `$(System.AccessToken)` and set
   `SAFO_ADO_TOKEN_KIND=bearer`.
2. Otherwise the caller's own `az login`: `az account get-access-token --resource 499b84ac-1321-427f-aa17-267ca6975798`.

`SAFO_ADO_URL` points a token from `SAFO_ADO_TOKEN` at Azure DevOps Server (`https://tfs.example.com/tfs`; `project.owner`
is then the collection). A token from `az` is only ever sent to `https://dev.azure.com`. The client refuses redirects,
answers a sign-in page as a refused token, and never puts the token in a message.

Scopes a personal access token needs:

| Command | Scopes |
|---|---|
| `audit`, `agents-status`, `status --print` | Work Items (Read), Code (Read), Project and Team (Read) |
| `reconcile`, `sync` | Work Items (Read & write), Code (Read), Project and Team (Read) |
| `status --post` | the above, and Wiki (Read & write) |
| `bootstrap` | Work Items (Read, write & manage), Project and Team (Read & write); adding states and fields to the process also needs a Project Collection Administrator |

Run `bootstrap` by hand, as an administrator, with `az login`. Run `reconcile` on a schedule with the least token that
does it.

## In Azure Pipelines

```yaml
schedules:
  - cron: "17 * * * *"
    displayName: reconcile the board
    branches: {include: [main]}
    always: true

pool: {vmImage: ubuntu-latest}

steps:
  - checkout: self
  - script: |
      python3 -m venv .safo
      .safo/bin/pip install --require-hashes -r safo/requirements.txt
      .safo/bin/pip install --no-deps ./safo
    displayName: install safo
  - script: .safo/bin/safo --board board.yaml reconcile
    displayName: safo reconcile
    env:
      SAFO_ADO_TOKEN: $(System.AccessToken)
      SAFO_ADO_TOKEN_KIND: bearer
```

The project's build service identity needs "Edit work items in this node" on the areas it writes. The same trust rule
as the GitHub Action holds: never run untrusted code (a pull request's build) in the job before SAFO.

## The dispatch guard

A brief may name an Azure Boards work item with `issue: AB#123`. Like a Jira key, it is accepted on its shape: the hook
has seconds and `az` takes longer than that to start. `safo audit` on the Azure DevOps board checks the work items
themselves.

## Not on Azure DevOps

* The waterfall process method (`process.method: waterfall`, phases and gates).
* Force pushes: on Azure Repos they are a Git permission, not a policy. `audit` says so and does not read them.
* `rules.gate_require_admin_enforced` and `rules.gate_check_app_id`: GitHub branch protection only.
* `protection` and `handoff` read GitHub only.

## Tested against

A loopback fake of the REST API (`tests/fakeado.py`), with the request and answer shapes of the Azure DevOps REST API
7.1. It has not yet been run against a live organization; the first live run should be `safo --dry-run bootstrap` and
`safo audit`.
