"""Prepaid-Guthaben (Wallet) für voicehook: SQLite-Ledger, Preislogik, Stripe-Anbindung.

Portiert aus voicehook-v3 (apps/agent/billing, Issue #53) und an v4 angepasst:
Konto = E-Mail aus Stripe Checkout, Zugriff über ein geheimes Wallet-Token,
Abbuchung = echter Verbrauch x Faktor (siehe pricing.py).
"""
