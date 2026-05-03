export interface InvoicePayload {
  customer_id: string;
  total: number;
}

export async function postInvoice(customerId: string,
                                  items: object[]): Promise<InvoicePayload> {
  const res = await fetch(`/api/invoice`, {
    method: "POST",
    body: JSON.stringify({ customer_id: customerId, items }),
  });
  return res.json();
}
