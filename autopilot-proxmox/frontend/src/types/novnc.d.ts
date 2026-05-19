declare module "@novnc/novnc" {
  interface RfbOptions {
    readonly credentials?: {
      readonly password?: string;
    };
    readonly wsProtocols?: readonly string[];
  }

  export default class RFB extends EventTarget {
    constructor(target: HTMLElement, urlOrChannel: string, options?: RfbOptions);

    viewOnly: boolean;
    scaleViewport: boolean;
    resizeSession: boolean;

    disconnect(): void;
    focus(): void;
    sendCtrlAltDel(): void;
    sendCredentials(credentials: { readonly password?: string }): void;
  }
}
