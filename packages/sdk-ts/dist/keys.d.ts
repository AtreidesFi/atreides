export declare interface ApiKeyFile {
    readonly v: 1;
    readonly keyId: string | null;
    readonly privateKey: string;
    readonly publicKey: string;
    readonly createdAt: string;
    readonly apiOrigin: string;
}

export declare function boundKey(origin: string, file: ApiKeyFile): Key;

export declare function generateKeyPair(origin: string, now?: () => number): Promise<ApiKeyFile>;

declare interface Key {
    readonly keyText: string;
    readonly seed: Uint8Array;
}

export declare function keyRequestLink(appOrigin: string, key: ApiKeyFile, name: string, scopes: readonly KeyScope[]): string;

export declare type KeyScope = 'read' | 'trade';

export declare function parseKeyFile(value: unknown): ApiKeyFile;

export { }
