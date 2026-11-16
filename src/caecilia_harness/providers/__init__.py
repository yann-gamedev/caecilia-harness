"""Model provider adapters implementing core.ports.ModelProvider.

`fake` is the scripted provider for tests; `deepseek` is the first real provider (decision D2).
Every real adapter authenticates with an API key only and names the provider's terms page in
its module docstring (ADR-0007).
"""
