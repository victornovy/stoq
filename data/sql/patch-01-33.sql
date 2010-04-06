-- #4111: Criação de relatório (contas a receber + contas a pagar)

CREATE TABLE payment_flow_history (
    id serial NOT NULL PRIMARY KEY,
    te_created_id bigint UNIQUE REFERENCES transaction_entry(id),
    te_modified_id bigint UNIQUE REFERENCES transaction_entry(id),

    history_date timestamp,

    to_receive numeric(10, 2) CONSTRAINT positive_value_to_receive CHECK (to_receive >= 0),
    received numeric(10, 2) CONSTRAINT positive_value_received CHECK (received >= 0),
    to_pay numeric(10, 2) CONSTRAINT positive_value_to_pay CHECK (to_pay >= 0),
    paid numeric(10, 2) CONSTRAINT positive_value_paid CHECK (paid >= 0),

    balance_expected numeric(10, 2),
    balance_real numeric(10, 2)
);
