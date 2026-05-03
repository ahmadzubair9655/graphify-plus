import * as React from "react";

export interface CheckoutButtonProps {
  customerId: string;
  onCheckout: (id: string) => void;
}

export function CheckoutButton(props: CheckoutButtonProps): JSX.Element {
  return <button onClick={() => props.onCheckout(props.customerId)}>Checkout</button>;
}

export default CheckoutButton;
