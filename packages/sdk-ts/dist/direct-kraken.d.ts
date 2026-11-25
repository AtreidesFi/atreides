/** Prefix + the bot's id, cut to the venue's length. UUID-shaped spot ids are left alone: a prefix would break them. */
export declare function krakenClientId(product: KrakenProduct, prefix: string, id: string): string;

export declare class KrakenDirect {
    #private;
    readonly options: KrakenDirectOptions;
    readonly venue: "kraken";
    constructor(options: KrakenDirectOptions);
    tag(): {
        readonly venue: "kraken";
        readonly kind: "body_field";
        readonly name: "broker";
        readonly value: string;
        readonly signed: true;
    };
    /**
     * Spot: pass the form parameters (without nonce) of AddOrder or AddOrderBatch. Futures: the query parameters of
     * sendorder or batchorder. Other paths are returned unchanged. While the program is live our `broker` replaces one the
     * bot set (with a one-time notice); while it is not, the bot's own `broker` is left as it is. AddOrderBatch goes to
     * Kraken as a JSON body: pass `orders` as its JSON text and put the parsed array back into that body.
     */
    prepare(product: KrakenProduct, path: string, params: KrakenParams): KrakenParams;
}

export declare interface KrakenDirectOptions {
    /** Our partner IIBAN from config, sent only when `programLive` is true (CEX-504). */
    readonly broker: string;
    readonly programLive: boolean;
    /** Default true. */
    readonly prefixClientIds?: boolean;
    /** Neutral string from `venues.kraken.sdk_client_prefix`; default 'a1'. */
    readonly clientPrefix?: string;
    readonly warn?: (message: string) => void;
}

export declare type KrakenParams = ReadonlyArray<readonly [string, string]>;

export declare type KrakenProduct = 'spot' | 'futures';

export { }
