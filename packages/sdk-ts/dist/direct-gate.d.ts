export declare const GATE_CHANNEL_HEADER = "X-Gate-Channel-Id";

export declare class GateDirect {
    #private;
    readonly options: GateDirectOptions;
    readonly venue: "gate";
    constructor(options: GateDirectOptions);
    tag(): {
        readonly venue: "gate";
        readonly kind: "header";
        readonly name: "X-Gate-Channel-Id";
        readonly value: string;
        readonly signed: false;
    };
    /** Tags one REST request. Only order-create paths change; no other field of the bot's request is touched. */
    prepare(request: GateOutgoing): GatePrepared;
    /** WebSocket order channels: never tagged. Warns once per instance. */
    wsOrder(): {
        readonly tagged: false;
    };
}

export declare interface GateDirectOptions {
    /** Our channel id from config; '' means the program is not live and nothing is sent. */
    readonly channelId: string;
    /** Default true. Adds the client id prefix to `text` on order paths. */
    readonly prefixClientIds?: boolean;
    /** Neutral string from `venues.gate.sdk_client_prefix`; default 'a1'. */
    readonly clientPrefix?: string;
    readonly warn?: (message: string) => void;
}

export declare interface GateOutgoing {
    readonly method: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
    /** Resource path including /api/v4. */
    readonly path: string;
    readonly body?: string;
}

export declare interface GatePrepared {
    readonly body: string | undefined;
    /** Unsigned extra headers; merge with the signature headers. */
    readonly headers: Readonly<Record<string, string>>;
}

/** `t-` + prefix + the bot's id, cut to 28 bytes after `t-`. */
export declare function gateText(prefix: string, id: string): string;

export { }
