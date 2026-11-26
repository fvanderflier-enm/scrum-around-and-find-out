# SPDX-License-Identifier: Apache-2.0
"""Azure DevOps: Azure Boards as the board, Azure Repos as the repositories.

A board file with `project.platform: azure-devops` is read by this package instead of the GitHub client: the work items
of one Azure DevOps project are the cards, its pull requests are checked for linked work items, and the fields, states,
area paths, iterations and shared queries that board.yaml names are kept in line with the project's inherited process.
See docs/azure-devops.md for the mapping.
"""
