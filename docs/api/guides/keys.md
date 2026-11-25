# API keys and scopes

In API & Bots, request read access for account and market queries. Add trade access when the bot needs to send orders.

## trade-grant

A trade grant binds the eligible venue accounts and limits. Review those details in the browser before signing the grant.

The CLI can generate the private seed locally with:

    atreides keys create --scopes read --name first-bot

Confirm the key request in the browser, then run the bind command against the same API origin and verify the binding and clock.

    atreides keys bind <keyText>
    atreides ping

Store the key file with mode 0600. Rotate through API & Bots, switch the bot to the new key and revoke the old one. Revoking an API key disables its API access. Review and revoke any separate venue credential at the venue or through Connections.
