export declare interface BitgetRestRequest {
    readonly method: string;
    /** Path without the query. */
    readonly path: string;
    readonly headers?: Readonly<Record<string, string>>;
    /** An object for `place-order`, an array of orders for `place-batch`. */
    readonly body?: Json;
}

export declare interface BitgetWsFrame {
    readonly op: string;
    readonly topic?: string;
    readonly args: readonly Json[];
    readonly [key: string]: Json | undefined;
}

/** Our program code for one venue, from config. An empty `value` means the program is not live: nothing is sent. */
declare interface CexTag {
    readonly kind: CexTagKind;
    readonly name: string;
    readonly value: string;
}

declare type CexTagKind = 'body_field' | 'header' | 'ws_field' | 'ws_header';

export declare interface DirectBitget {
    tag(): CexTag;
    /** `place-order` and `place-batch` get the header and the id prefix; everything else passes through. */
    rest(request: BitgetRestRequest): BitgetRestRequest;
    /** `trade` frames with topic `place-order` or `batch-place` get top-level `apiCode` and the id prefix. */
    ws(frame: BitgetWsFrame): BitgetWsFrame;
}

export declare function directBitget(tag: CexTag, options?: DirectCexOptions): DirectBitget;

declare interface DirectCexOptions {
    /** Default true: prefix the bot's own client id. */
    readonly prefixClientIds?: boolean;
    /** `venues.<v>.sdk_client_prefix` from config; default `a1`. */
    readonly clientPrefix?: string;
    /** Default console.warn. Called once each time a bot's own tag value is replaced by ours. */
    readonly onNotice?: (message: string) => void;
}

declare type Json = null | boolean | number | string | readonly Json[] | {
    readonly [key: string]: Json;
};

export { }
