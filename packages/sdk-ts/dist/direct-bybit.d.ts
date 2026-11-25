export declare interface BybitRestRequest {
    readonly method: string;
    /** Path without the query. */
    readonly path: string;
    readonly headers?: Readonly<Record<string, string>>;
    readonly body?: JsonObject;
}

export declare interface BybitWsFrame {
    readonly op: string;
    readonly header?: Readonly<Record<string, string>>;
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

export declare interface DirectBybit {
    tag(): CexTag;
    /** Create and batch-create get `Referer` and the id prefix; every other request passes through unchanged. */
    rest(request: BybitRestRequest): BybitRestRequest;
    /** `order.create` and `order.create-batch` get `header.Referer` and the id prefix. */
    ws(frame: BybitWsFrame): BybitWsFrame;
}

export declare function directBybit(tag: CexTag, options?: DirectCexOptions): DirectBybit;

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

declare type JsonObject = {
    readonly [key: string]: Json;
};

export { }
