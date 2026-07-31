/**
 * The MQTT implementation of `MessagingClient`.
 *
 * Everything transport-specific lives here. Reached through
 * `createMessagingClient` in messaging.ts rather than imported by components.
 */

import mqtt from 'mqtt';
import type { IClientOptions, MqttClient } from 'mqtt';
import { getClientId, generateId, loadConfig, type AppConfig } from './config';
import { dispatchMessage } from './dispatch';
import type { MessagingClient } from './messaging';
import { connection, pushNotice } from './stores';
import {
  KITCHEN_RESET,
  KITCHEN_STATUS,
  TABLE_STATE_SUBSCRIPTION,
  clientErrorTopic,
  orderTopic,
} from './topics';
import type { OrderPlaced } from './types';

const PUBLISH_TIMEOUT_MS = 8000;

export function connectMqtt(config: AppConfig = loadConfig()): MessagingClient {
  const clientId = getClientId();

  const options: IClientOptions = {
    clientId,
    username: config.username,
    password: config.password,
    // MQTT 3.1.1. Chosen for the widest broker compatibility; nothing in this
    // app needs an MQTT 5 feature.
    protocolVersion: 4,
    clean: true,
    reconnectPeriod: 2000,
    connectTimeout: 10_000,
    // Re-subscribe automatically after a reconnect. Combined with retained state
    // topics this means a reconnect re-delivers the full picture with no
    // application-level resync logic.
    resubscribe: true,
  };

  const client: MqttClient = mqtt.connect(config.brokerUrl, options);

  client.on('connect', () => {
    connection.set('connected');
    client.subscribe(
      [TABLE_STATE_SUBSCRIPTION, KITCHEN_STATUS, clientErrorTopic(clientId)],
      { qos: 1 },
      (error) => {
        if (error) {
          console.error('[mqtt] subscribe failed', error);
          pushNotice('Could not subscribe to kitchen updates.', 'error');
        }
      },
    );
  });

  client.on('reconnect', () => connection.set('reconnecting'));
  client.on('offline', () => connection.set('offline'));
  client.on('close', () => {
    // `close` also fires between reconnect attempts, so do not overwrite the
    // more informative 'reconnecting' state with 'offline' here.
    connection.update((current) =>
      current === 'reconnecting' ? current : 'offline',
    );
  });

  client.on('error', (error) => {
    // mqtt.js reconnects on its own; surfacing the error is enough.
    console.error('[mqtt] client error', error);
  });

  client.on('message', (topic, payload, packet) => {
    // packet.retain distinguishes "the broker's stored last value" from "this just
    // happened", which the clock-offset estimate depends on.
    dispatchMessage(topic, payload.toString('utf8'), clientId, {
      retained: packet.retain === true,
    });
  });

  /** Publish and wait for the broker's acknowledgement, or fail loudly. */
  function publish(topic: string, body: string): Promise<void> {
    return new Promise<void>((resolve, reject) => {
      // Without this, a broker that never acknowledges leaves the UI waiting on a
      // promise that can never settle.
      const timer = setTimeout(
        () =>
          reject(new Error('Timed out waiting for the broker to accept the request.')),
        PUBLISH_TIMEOUT_MS,
      );
      client.publish(topic, body, { qos: 1, retain: false }, (error) => {
        clearTimeout(timer);
        if (error) reject(error);
        else resolve();
      });
    });
  }

  async function requestReset(): Promise<void> {
    await publish(KITCHEN_RESET, JSON.stringify({ requestedBy: clientId }));
  }

  async function publishOrder(tableId: number, foodName: string): Promise<void> {
    const order: OrderPlaced = {
      // Generated per attempt and reused on retransmission by the MQTT layer, so
      // the backend can recognise a duplicate delivery of this same order.
      clientOrderId: generateId(),
      clientId,
      tableId,
      foodName,
      sentAt: new Date().toISOString(),
    };

    await publish(orderTopic(tableId), JSON.stringify(order));
  }

  async function disconnect(): Promise<void> {
    await client.endAsync();
  }

  return { clientId, publishOrder, requestReset, disconnect };
}
