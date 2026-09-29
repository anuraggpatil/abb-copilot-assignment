"""One module per group of tools, each exposing `register(server)`.

Grouped by the question they answer rather than by the endpoint they call, because the tool
catalogue is read by a planner choosing what to do next — not by someone browsing the API.
"""
