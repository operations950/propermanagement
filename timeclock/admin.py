from django.contrib import admin

from . import models

for model in (models.TimeProfile, models.Punch, models.TimeOff, models.ScheduleChange, models.PunchRequest, models.HolidayPay, models.PayPeriodLock,
              models.PolicySignature, models.OfficeConnection):
    admin.site.register(model)
