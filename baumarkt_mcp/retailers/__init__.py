"""Retailer adapters for baumarkt-mcp.

Package marker only — deliberately does not import the individual retailer
modules (``hornbach``, ``bauhaus``, ``globus``, ``obi``). Each is built
independently and importing them eagerly here would break any of them being
worked on before the others exist.
"""
