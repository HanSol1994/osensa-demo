/// <reference types="svelte" />
/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Which transport to build. Only "mqtt" exists today. */
  readonly VITE_MESSAGING_BACKEND?: string;
  /** Broker WebSocket URL, e.g. ws://localhost:9001/mqtt or wss://host:8884/mqtt */
  readonly VITE_MQTT_URL?: string;
  readonly VITE_MQTT_USERNAME?: string;
  readonly VITE_MQTT_PASSWORD?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
