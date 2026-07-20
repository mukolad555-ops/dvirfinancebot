-- DvirFinance: повне обнулення робочих даних
-- Запустити ОДИН раз у Supabase → SQL Editor перед початком роботи.
begin;

truncate table public.debt_payments restart identity cascade;
truncate table public.customer_debts restart identity cascade;
truncate table public.daily_closing_accounts restart identity cascade;
truncate table public.daily_closings restart identity cascade;
truncate table public.cash_operations restart identity cascade;
truncate table public.cash_revisions restart identity cascade;
truncate table public.cash_accounts restart identity cascade;

commit;
