import { HttpClient } from '@angular/common/http';
import { Injectable } from '@angular/core';

export const routes = [
  { path: 'orders/new', component: 'CreateOrderComponent' },
  { path: 'orders/:id', component: 'OrderDetailComponent' },
  { path: 'shipments/:id', component: 'ShipmentPageComponent' },
];

export const createOrderTemplate = `
<form [formGroup]="form">
  <input formControlName="customerId" />
  <button type="button" (click)="submitOrder()">Submit</button>
</form>
`;

export interface CreateOrderRequest { customerId: number; lines: unknown[]; }

@Injectable({ providedIn: 'root' })
export class OrderService {
  constructor(
    private readonly http: HttpClient,
    private readonly config: any,
  ) {}

  createOrder(request: CreateOrderRequest) {
    return this.http.post(this.config.createOrder, request);
  }

  readOrder(orderId: string) {
    return this.http.get(`${this.config["getOrderDetail"]}/${orderId}`);
  }

  cancelOrder(orderId: string) {
    const url = `${this.config.orderDetail}/${orderId}`;
    return this.http.delete(url);
  }

  retryOrder(request: CreateOrderRequest) {
    return this.http.request('POST', this.config['retryOrder'], { body: request });
  }

  patchStatus(orderId: string, status: string) {
    const { updateStatus } = this.config;
    const method = 'PATCH';
    return this.http.request(method, `${updateStatus}/${orderId}/status`, { body: { status } });
  }

  confirmShipment(shipmentId: string) {
    return this.http.post(`${this.config.confirmShipment}/${shipmentId}/confirm`, {});
  }
}

export class CreateOrderComponent {
  form = {};
  constructor(private readonly service: OrderService) {}
  submitOrder() {
    const request = { customerId: 100, lines: [] };
    return this.service.createOrder(request);
  }
}

export class OrderDetailComponent {
  constructor(private readonly service: OrderService) {}
  load(orderId: string) {
    return this.service.readOrder(orderId);
  }
  cancelOrder(orderId: string) {
    return this.service.cancelOrder(orderId);
  }
  retryOrder(request: CreateOrderRequest) {
    return this.service.retryOrder(request);
  }
  patchStatus(orderId: string, status: string) {
    return this.service.patchStatus(orderId, status);
  }
}

export class ShipmentPageComponent {
  constructor(private readonly service: OrderService) {}
  confirmShipment(shipmentId: string) {
    return this.service.confirmShipment(shipmentId);
  }
}

export const legacyOrderRoute = '/legacy/orders';

export const ambiguousRoutes = [
  'GET /api/orders/{id}',
  'GET /orders/{id}',
];

export class DynamicOrderCaller {
  constructor(private readonly http: HttpClient, private readonly config: any) {}
  call(endpointName: string, body: unknown) { return this.http.post(this.config[endpointName], body); }
}
