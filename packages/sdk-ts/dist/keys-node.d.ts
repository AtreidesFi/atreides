declare interface ApiKeyFile {
    readonly v: 1;
    readonly keyId: string | null;
    readonly privateKey: string;
    readonly publicKey: string;
    readonly createdAt: string;
    readonly apiOrigin: string;
}

export declare function bindKeyFile(path: string, keyText: string, fetch?: typeof globalThis.fetch): Promise<ApiKeyFile>;

export declare function readKeyFile(path: string): Promise<ApiKeyFile>;

export declare function writeKeyFile(path: string, value: ApiKeyFile, replaceExisting?: boolean): Promise<void>;

export { }
