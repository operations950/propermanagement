"""The recurring payables the owner approved from the statement review (Proposed_Recurring_Payables_by_Bucket.xlsx, 2026-11-03), ready for load_recurring_payables.

One row per recurring bill: (association key, vendor, description, expense account, amount or None when it changes, how paid, text the bank statement shows, day of the month,
active). `active` is False where the review left a question open (an irregular month, a changed amount). The weekly payroll, a credit card payment and an unnamed pest control
company are left out on purpose: the recurring bill here is monthly or longer, needs a payee, and posts to an expense account.
"""

# association key -> words every word of which must appear in the association's name (lower case)
ASSOCIATIONS = {
    'grand': ('andrews', 'grand'),
    'glen': ('andrews', 'glen'),
    'pensee': ('pensee',),
    'lakeside2': ('lakeside', '2'),
    'lakeside8': ('lakeside', '8'),
    'castlewood': ('castlewood',),
}

# how a name that the ledger cut short is written out in full (the load looks for an existing vendor first, so this only matters for a vendor that is new)
FULL_NAMES = {
    'Palm Beach Pest Man...': 'Palm Beach Pest Management',
    'Forever Green Landsca...': 'Forever Green Landscape Services',
}

# (association, vendor, description, account, amount, how paid, statement text, day, active)
ROWS = [
    # ---------------------------------------------------------------- Castlewood Town Villas
    ('castlewood', 'Johnny All Around', 'Lawn maintenance', '6893', '1000.00', 'check', '', 15, True),
    ('castlewood', 'True Blue Pools', 'Pool service', '6889', '390.00', 'check', '', 15, False),
    ('castlewood', 'Proper Realty Co', 'Management fee', '6820', '1000.00', 'check', '', 15, True),
    ('castlewood', 'Orkin', 'Pest control', '6906', None, 'check', '', 15, True),
    ('castlewood', 'FPL', 'Electricity', '6410', None, 'ach', 'FPL', 28, True),
    ('castlewood', 'Seacoast Utility Authority', 'Water and sewer', '6430', None, 'ach', 'SEACOAST', 28, True),
    # ---------------------------------------------------------------- La Pensee
    ('pensee', 'Beachkeeper, Inc', 'Beach cleaning', '6905', '375.00', 'check', '', 16, True),
    ('pensee', 'Goldring Protection', 'Video intercom access control', '6897', '159.60', 'check', '', 9, True),
    ('pensee', 'Palm Beach Pest Man...', 'Pest control (monthly)', '6906', '225.00', 'check', '', 11, True),
    ('pensee', 'Palm Beach Pest Man...', 'Landscape pest control (every other month)', '6906', '200.00', 'check', '', 9, False),
    ('pensee', 'Proper Realty Co', 'Management fee', '6820', '3750.00', 'check', '', 14, True),
    ('pensee', 'AT&T', 'Phone and internet', '6425', '99.35', 'ach', 'ATT', 25, True),
    ('pensee', 'AT&T', 'Phone and internet (second account)', '6425', None, 'ach', 'ATT', 4, True),
    ('pensee', 'Comcast', 'Office internet', '6440', '128.67', 'ach', 'COMCAST', 6, True),
    ('pensee', 'Comcast', 'Bulk cable', '6440', '2077.97', 'ach', 'COMCAST', 11, True),
    ('pensee', 'FPL', 'Electricity (fixed charge)', '6410', '38.38', 'ach', 'FPL DIRECT DEBIT', 11, True),
    ('pensee', 'FPL', 'Electricity', '6410', None, 'ach', 'FPL DIRECT DEBIT', 9, True),
    ('pensee', 'Nextera Energy', 'Electricity (Nextera)', '6410', None, 'ach', 'NEXTERA', 15, True),
    ('pensee', 'Frontline Insurance', 'Insurance installment', '6882', '12428.59', 'ach', '', 8, True),
    ('pensee', 'Jan-Pro of Central Florida', 'Janitorial', '6912', '505.88', 'ach', 'JANPROOFCENTRAL', 18, True),
    ('pensee', 'Sure Payroll', 'Payroll processing fee (fixed)', '6221', '41.28', 'ach', 'PAYROLL', 3, False),
    ('pensee', 'Sure Payroll', 'Payroll processing and maintenance payroll', '6221', None, 'ach', 'PAYROLL', 3, True),
    ('pensee', 'The Pool Scientist', 'Pool service', '6889', '485.00', 'ach', 'POOL S', 6, False),
    ('pensee', 'Waste Management', 'Trash', '6450', '273.87', 'ach', 'WASTE MANAGEMENT', 16, False),
    ('pensee', 'City of West Palm Beach', 'Water, sewer and city charges', '6430', None, 'ach', 'CITYOFWESTPALM', 24, True),
    ('pensee', 'FPU EZ-Bill Gas Utility', 'Gas', '6420', None, 'ach', 'FPUC', 22, True),
    ('pensee', 'Zenith', "Workers' compensation", '6883', None, 'ach', 'ZENITH', 17, False),
    # ---------------------------------------------------------------- Lakeside Point #8
    ('lakeside8', 'Proper Realty Co', 'Management fee', '6820', '450.00', 'check', '', 15, True),
    ('lakeside8', 'Carleton Lakeside LLC', 'Long-term lease', '6205', '211.50', 'ach', 'TO DREW', 2, True),
    ('lakeside8', 'Nancy Bowyer', 'Long-term lease', '6205', '211.50', 'ach', 'TO NANCY', 2, True),
    ('lakeside8', 'IPFS Corporation', 'Insurance premium financing', '6882', '3135.92', 'ach', '866-223-4478', 17, True),
    ('lakeside8', 'City of Lake Worth Beach', 'Water and sewer', '6430', None, 'ach', 'CITYOFLAKEWORTH', 8, True),
    # ---------------------------------------------------------------- Lakeside Point Gardens #2
    ('lakeside2', 'Proper Realty Co', 'Management fee', '6820', '500.00', 'check', '', 14, True),
    ('lakeside2', 'Carleton Lakeside LLC', 'Long-term lease', '6205', '187.50', 'ach', '', 15, True),
    ('lakeside2', 'Nancy Bowyer', 'Long-term lease', '6205', '187.50', 'ach', '', 15, True),
    ('lakeside2', 'IPFS Corporation', 'Insurance premium financing', '6882', '2969.47', 'ach', '', 20, True),
    ('lakeside2', 'Truly Nolen', 'Pest control', '6906', '41.00', 'ach', '', 19, True),
    ('lakeside2', 'City of Lake Worth Beach', 'Water and sewer', '6430', None, 'ach', '', 17, True),
    # ---------------------------------------------------------------- St. Andrews Glen (its first five are already loaded; the load leaves them and reports any difference)
    ('glen', 'ABA Pool Service', 'Pool service', '6889', '300.00', 'ach', 'ABA POOL', 20, True),
    ('glen', 'Cellgate', 'Gate access service', '6915', '274.77', 'ach', 'CELLGATE', 22, True),
    ('glen', 'FIRST Insurance Funding', 'Insurance premium financing', '6882', '9497.92', 'ach', 'INSURANCE FIRST', 18, True),
    ('glen', 'Forever Green Landsca...', 'Landscaping', '6893', '2435.00', 'ach', '', 12, True),
    ('glen', 'Hotwire Communications', 'Internet and cable', '6440', '4016.69', 'ach', 'HOTWIRE', 11, True),
    ('glen', 'Proper Realty Co', 'Management fee', '6820', '1100.00', 'check', '', 14, True),
    ('glen', 'FPL', 'Electricity', '6410', None, 'ach', 'FPL', 28, True),
    ('glen', 'Palm Beach County', 'Water and sewer', '6430', None, 'ach', 'WATER BILL', 3, True),
    # ---------------------------------------------------------------- St. Andrews Grand
    ('grand', 'Proper Realty Co', 'Management fee', '6820', '1400.00', 'check', '', 9, True),
    ('grand', 'Rust Tech Services', 'Water treatment (iron control)', '6898', '303.19', 'check', '', 24, True),
    ('grand', 'AFCO', 'Insurance premium financing', '6882', '8386.39', 'ach', 'AFCO', 20, True),
    ('grand', 'FPL', 'Electricity', '6410', None, 'ach', 'FPL DIRECT DEBIT', 24, True),
]

# What was left out of the load, and why (printed at the end of every run)
LEFT_OUT = [
    'La Pensee: Sure Payroll weekly payroll of $1,030.86 (a bill here is monthly or longer; the monthly Sure Payroll bill above reads the payroll debits from the bank statement)',
    'La Pensee: Truist credit card payment (not a vendor bill: it settles the card)',
    'Lakeside Point #8: pest control at $109.00 (the payee is not known yet)',
    'St. Andrews Grand: Mignano lawn care, and the three City of West Palm Beach fixed lines at La Pensee (taken off the list in the review)',
]
