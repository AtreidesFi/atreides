declare interface GrantAccount {
    readonly venue: string;
    readonly accountRef: string;
}

/** The fields of the grant text as 03 §5.3 (C28) fixes them: instants are RFC 3339 ms Z strings, an empty list is `any`. */
export declare interface GrantFields {
    readonly origin: string;
    readonly keyText: string;
    readonly publicKey: string;
    readonly accounts: readonly GrantAccount[];
    readonly ipAllowlist: readonly string[];
    readonly expiresAt: string;
    readonly nonce: string;
    readonly issuedAt: string;
}

/** The exact text of §5.5, accounts sorted, lines joined by "\n", no trailing newline (C28). */
export declare function grantMessage(input: GrantFields): string;

export declare type GrantScheme = 'eip191' | 'solana' | 'adr36';

/** The canonical request of 03 §5.3. `target` and `body` are the actual request target and bytes. */
export declare interface RequestToSign {
    readonly method: string;
    readonly target: string;
    readonly body: Uint8Array;
    readonly timestampMs: string;
    readonly nonce: string;
    readonly keyText: string;
}

export declare function signedString(request: RequestToSign): Uint8Array;

export declare function signRequest(request: RequestToSign, seed: Uint8Array): string;

/**
 * Offline only (C28): EIP-191 recovery (secp256k1), Solana Ed25519 over the UTF-8 bytes, ADR-36 amino sign doc. No provider,
 * RPC, EIP-1271 or remotely supplied verification result.
 */
export declare function verifyGrant(text: string, signature: string, signer: string, scheme: GrantScheme): boolean;

export declare function verifyRequest(request: RequestToSign, signature: string, publicKey: Uint8Array): boolean;

export { }
