"""The chart of accounts a new association starts from.

Numbers follow Yardi's, so an old report and a new one line up. Yardi's own list is the whole company's template: 230 lines, many left over from
other properties (New York rent credits, old bank loans, an account marked "DO NOT USE"). This is the part an association actually uses.
"""

ASSET, LIABILITY, EQUITY, INCOME, EXPENSE = 'asset', 'liability', 'equity', 'income', 'expense'

# the accounts the program itself posts to
CASH_OPERATING = '1110'
CASH_RESERVE = '1120'
AR = '1300'
DUE_FROM_OPERATING = '1893'
AP = '2200'
DUE_TO_RESERVES = '2207'
PREPAID_FEES = '2210'
FUND_BALANCE = '3070'
RETAINED_EARNINGS = '3800'
POOLED_RESERVES = '3827'
INCOME_ASSESSMENT = '4500'
INTEREST_INCOME = '4511'
INCOME_SPECIAL = '4513'
INCOME_MISC = '4700'
INCOME_LATE_FEE = '4702'
BANK_FEES = '6510'
MANAGEMENT_FEES = '6820'
RESERVE_TRANSFER = '6900'
UNCATEGORIZED = '6902'

CHART = [
    # assets
    ('1110', 'Operating Cash', ASSET), ('1111', 'Operating Cash #2', ASSET), ('1120', 'Escrow Cash', ASSET), ('1121', 'Escrow Cash #2', ASSET),
    ('1125', 'Petty Cash', ASSET), ('1130', 'Reserve Fund Cash', ASSET),
    ('1300', 'Accounts Receivable', ASSET), ('1310', 'Allowance for Doubtful Accounts', LIABILITY),
    ('1891', 'Prepaid Expenses', ASSET), ('1892', 'Prepaid Insurance', ASSET), ('1893', 'Due from Operating', ASSET), ('1895', 'Utility Deposits', ASSET),
    ('1896', 'Advance Insurance Reserve Disb', ASSET),
    # liabilities
    ('2200', 'Accounts Payable', LIABILITY), ('2207', 'Due to Reserves', LIABILITY), ('2210', 'Prepaid Fees', LIABILITY),
    ('2213', 'A/P Insurance', LIABILITY), ('2250', 'Owner Deposits', LIABILITY), ('2251', 'Key Deposit', LIABILITY), ('2300', 'Premium Insurance Loan', LIABILITY),
    ('2320', 'Accrued Expense', LIABILITY), ('2400', 'Deferred Assessment', LIABILITY), ('2410', 'Deferred Special Assessment', LIABILITY),
    # equity and the reserve funds
    ('3060', 'Operating Loan Fund', EQUITY), ('3070', 'Fund Balance - Operating', EQUITY), ('3080', 'Prior Period Adjustments', EQUITY), ('3800', 'Retained Earnings', EQUITY),
    ('3811', 'Reserves Fund 3 Wire Meter Stack', EQUITY), ('3812', 'Reserves Fund Generator', EQUITY), ('3813', 'Reserves Fund Interest', EQUITY),
    ('3814', 'Reserve Sewer/Plumbing', EQUITY), ('3815', 'Reserve Automatic Gates/ Motors', EQUITY), ('3816', 'Reserve Gutters/Downspouts', EQUITY),
    ('3817', 'Reserve Lighting Fixtures/ Lamp Post', EQUITY), ('3818', 'Reserve Patio Enclosures', EQUITY), ('3819', 'Reserve Tree Trimming', EQUITY),
    ('3820', 'Reserve Pool', EQUITY), ('3821', 'Reserve Seawall/Docks', EQUITY), ('3823', 'Reserve Electrical', EQUITY), ('3825', 'Reserve Fund Elevator', EQUITY),
    ('3826', 'Reserve Fund Painting', EQUITY), ('3827', 'Pooled Reserves', EQUITY), ('3828', 'Reserves Fund Plate Glass', EQUITY),
    ('3832', 'Reserve Fund Asphalt Replacement', EQUITY), ('3833', 'Reserve Insurance Deductible', EQUITY), ('3834', 'Reserve Fund Roof Replacement', EQUITY),
    ('3835', 'Reserve Insurance', EQUITY),
    # income
    ('4475', 'Prepaid Fees', INCOME), ('4500', 'Fees - Residential', INCOME), ('4510', 'Parking', INCOME), ('4511', 'Interest Income', INCOME),
    ('4512', 'Legal Fee Income', INCOME), ('4513', 'Special Assessments', INCOME), ('4514', 'Dock Fee Income', INCOME), ('4600', 'Application fees', INCOME),
    ('4700', 'Miscellaneous Income', INCOME), ('4701', 'Maintenance Assessments', INCOME), ('4702', 'Late Fee Income', INCOME), ('4810', 'NSF Fees', INCOME),
    ('4820', 'Bank Interest', INCOME),
    # expenses
    ('6206', 'Recreation Payment', EXPENSE), ('6207', 'Fitness Room Service', EXPENSE), ('6209', 'Irrigation - Parts and Repairs', EXPENSE),
    ('6210', 'Repair', EXPENSE), ('6220', 'Maintenance Personnel', EXPENSE), ('6221', 'Payroll Processing Fees', EXPENSE), ('6222', 'Security Guard Contract', EXPENSE),
    ('6230', 'Painting', EXPENSE), ('6240', 'HVAC', EXPENSE), ('6260', 'Gardening', EXPENSE), ('6320', 'Insurance', EXPENSE), ('6350', 'Property Tax', EXPENSE),
    ('6410', 'Electricity', EXPENSE), ('6420', 'Gas', EXPENSE), ('6425', 'Phone', EXPENSE), ('6430', 'Water and Sewer', EXPENSE), ('6431', 'Water - Irrigation', EXPENSE),
    ('6435', 'Sewer', EXPENSE), ('6440', 'Cable / Internet', EXPENSE), ('6450', 'Trash Disposal', EXPENSE), ('6460', 'Software', EXPENSE), ('6510', 'Bank Fees', EXPENSE),
    ('6610', 'Interest Expense', EXPENSE), ('6800', 'Administrative Expenses', EXPENSE), ('6820', 'Management Fees', EXPENSE), ('6830', 'Credit Card Fees', EXPENSE),
    ('6880', 'Miscellanous Expenses', EXPENSE), ('6882', 'Insurance - Hazard, Liability & Property', EXPENSE), ('6883', "Insurance- Workman's Compensation", EXPENSE),
    ('6884', 'Insurance - Flood', EXPENSE), ('6885', 'Insurance - D&O', EXPENSE), ('6886', 'Office Supplies & Equipment', EXPENSE),
    ('6887', 'Elevator Service', EXPENSE), ('6888', 'Elevator Repairs', EXPENSE), ('6889', 'Pool / Spa Service', EXPENSE), ('6890', 'Pool / Spa Repair', EXPENSE),
    ('6891', 'Licensing Fees', EXPENSE), ('6892', 'Security Gate Repairs', EXPENSE), ('6893', 'Landscaping - Maintenance', EXPENSE), ('6894', 'Legal Fees and Costs', EXPENSE),
    ('6895', 'Accounting', EXPENSE), ('6896', 'Fire Alarm - Sprinklers', EXPENSE), ('6897', 'Security System', EXPENSE), ('6898', 'General Maintenance', EXPENSE),
    ('6899', 'Background Check Fees', EXPENSE), ('6900', 'Reserve Transfer', EXPENSE), ('6901', 'Concrete Restoration', EXPENSE), ('6902', 'Uncategorized Expense', EXPENSE),
    ('6904', 'Bank Charges & Fees', EXPENSE), ('6906', 'Pest Control', EXPENSE), ('6909', 'Landscaping - Extras', EXPENSE), ('6910', 'Fire Alarm Repair & Maintenance', EXPENSE),
    ('6912', 'Building Janitorial', EXPENSE), ('6915', 'Security Gate', EXPENSE), ('6916', 'Audit Fee', EXPENSE), ('6918', 'Termite Control', EXPENSE),
    ('6919', 'Tree Trimming', EXPENSE), ('6920', 'Bad Debt Expense', EXPENSE),
]

# Report sections, by account number
def section(code):
    n = int(code)
    if n < 1200:
        return 'Cash'
    if n < 1400:
        return 'Receivables'
    if n < 2000:
        return 'Other assets'
    if n < 3000:
        return 'Liabilities'
    if n < 3800 or n == 3800:
        return 'Equity'
    if n < 4000:
        return 'Reserve funds'
    if n < 5000:
        return 'Income'
    return 'Expenses'
