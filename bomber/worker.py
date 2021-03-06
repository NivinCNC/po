import traceback
from functools import partial
import json
import logging
from collections import defaultdict
from itertools import cycle as CycleIter
from datetime import datetime, date, timedelta
from decimal import Decimal
import random
from copy import deepcopy
from math import ceil

import boto3
import bottle
from peewee import fn, SQL, JOIN_LEFT_OUTER, JOIN_INNER, R
from mongoengine import Q
from deprecated.sphinx import deprecated

from bomber.api import (
    AccountService,
    MessageService,
    AuditService,
    BillService,
    Dashboard,
    GoldenEye,
    Hyperloop,
    Message,
    Scout)
from bomber.constant_mapping import (
    AutoCallMessageCycle,
    ApplicationStatus,
    RealRelationship,
    BomberCallSwitch,
    CallActionCommit,
    ApplicantSource,
    ApplicationType,
    EscalationType,
    ApprovalStatus,
    AutoListStatus,
    AutoCallResult,
    BeforeInBomber,
    PriorityStatus,
    InboxCategory,
    OldLoanStatus,
    BombingResult,
    ContactStatus,
    SpecialBomber,
    PartnerStatus,
    Relationship,
    ConnectType,
    SubRelation,
    PhoneStatus,
    ContactType,
    SmsChannel,
    ContainOut,
    FIRSTLOAN,
    AppName,
    RipeInd,
    Cycle,
    ContactsUseful,
    DisAppStatus,
    BomberStatus,
    PartnerType)
from bomber.controllers.templates import cs_number_conf
from bomber.controllers.report_calculation.collection_tool import (
    average_call_duration_team
)
from bomber.controllers.report_calculation.collection_agent import get_agent
from bomber.db import db, readonly_db
from bomber.models_readonly import (
    DispatchAppHistoryR,
    AutoCallActionsR,
    ConnectHistoryR,
    ApplicationR,
    CallActionsR,
    OverdueBillR,
    BomberR)
from bomber.models import (
    ManualCallListStatus,
    RepaymentReportInto,
    OldLoanApplication,
    DispatchAppHistory,
    CompanyContactType,
    FamilyContactType,
    ReportCollection,
    RepaymentReport,
    AutoCallActions,
    DispatchAppLogs,
    ConnectHistory,
    BombingHistory,
    ManualCallList,
    AutoIVRActions,
    SummaryBomber,
    SummaryDaily,
    IVRCallStatus,
    BomberOverdue,
    AutoCallList,
    AutoIVRStatus,
    SystemConfig,
    RepaymentLog,
    IVRActionLog,
    TotalContact,
    Application,
    CallActions,
    DispatchApp,
    OverdueBill,
    Escalation,
    BomberPtp,
    WorkerLog,
    BomberLog,
    CycleList,
    Template,
    Transfer,
    Summary2,
    AutoIVR,
    Partner,
    Contact,
    CallLog,
    Summary,
    Bomber,
    Inbox,
    Role,
    SCI,
)
from bomber.sns import MessageAction, send_to_default_q
from bomber.utils import (
    get_cycle_by_overdue_days,
    str_no_utc_datetime,
    no_utc_datetime,
    gender_ktpnum,
    list_to_dict,
    birth_dt_ktp,
    number_strip,
    utc_datetime,
    OperatedDict,
    average_gen,
    time_logger,
    idg,
)
from bomber.report_work import get_every_cycle_report

app = bottle.default_app()
client = boto3.client('sqs')
#????????????dict,key-????????????v-????????????
actions = {}


def action(msg_action):
    action_name = msg_action.value.lower()
    if action_name not in actions:
        actions[action_name] = []

    def wrapper(func):
        actions[action_name].append(func)
        return func
    return wrapper


@action(MessageAction.BOMBER_HEALTH_CHECK)
def health_check(payload, msg_id):
    pass


def dpd1_classify(item, lst):
    app_name = str(item['app_name']).upper()
    key = '{}_{}_DPD1'.format(app_name, str(item['su']))
    if key in BeforeInBomber.keys():
        lst[key].append(item['id'])
    return lst


def dpd1_process(lst):
    """??????????????????"""
    if not lst:
        return

    for key, l in lst.items():
        rule = getattr(BeforeInBomber, key).value
        query = (AutoIVRActions
                 .select(fn.DISTINCT(AutoIVRActions.loanid))
                 .where(AutoIVRActions.loanid.in_(l),
                        AutoIVRActions.group.in_(rule.get('group')),
                        AutoIVRActions.callstate
                        .in_(IVRCallStatus.call_success())))
        success_set = {i.loanid for i in query}
        failed_list = list(set(l) - success_set)
        post_params = {
            '$and': rule.get('$and'),
            'app_list': failed_list
        }
        resp = Hyperloop().post("/bomber/score/verify", json=post_params)
        if not resp.ok:
            logging.error(
                'hyperloop score verification failed: %s, %s',
                str(resp.status_code),
                str(resp.text)
            )
            logging.error('hyperloop score verification failed: %s',
                          str(post_params))
            continue

        logging.debug('hyperloop score verification success: %s', resp.content)
        resp_json = resp.json()
        # dpd1 ????????????bomber
        app_list = resp_json['data']
        if not app_list:
            continue
        for item in app_list:
            # ???ab_test,??????????????????????????????
            if random.randint(0, 5) == 1:
                send_to_default_q(
                    MessageAction.APPLICATION_BOMBER,
                    {'id': int(item)}
                )


# auto_ivr,??????????????????
@action(MessageAction.GET_IVR)
def get_ivr(payload, msg_id):
    logging.warning('start get_ivr')
    sys_config = (SystemConfig.select()
                  .where(SystemConfig.key == 'DPD1-3_INTO_IVR')
                  .first())
    # ???????????????lid
    now = date.today()
    # ????????????????????????ivr,????????????????????????????????????????????????????????????????????????
    if sys_config and sys_config.value:
        start = now - timedelta(days=3)
    else:
        start = now
    end = now + timedelta(days=4)
    # TODO: ??????redis
    item = IVRActionLog.filter(IVRActionLog.proc_date == now).first()
    if not item:
        # ???????????????ivr??????
        AutoIVR.delete().execute()
        current_page = 0
    elif item.current_page >= item.total_page:
        return
    else:
        current_page = item.current_page

    #????????????   appname + ???????????? + ????????????
    auto_ivr = {
        'DanaCepat01': 1,
        'DanaCepat00': 2,
        'DanaCepat0PDP1': 3,
        'PinjamUang01': 4,
        'PinjamUang00': 5,
        'PinjamUang0PDP1': 6,
        'KtaKilat01': 7,
        'KtaKilat00': 8,
        'KtaKilat0PDP1': 9,
        'DanaCepat11': 10,
        'DanaCepat10': 11,
        'DanaCepat1PDP1': 12,
        'PinjamUang11': 13,
        'PinjamUang10': 14,
        'PinjamUang1PDP1': 15,
        'KtaKilat11': 16,
        'KtaKilat10': 17,
        'KtaKilat1PDP1': 18,
        'DanaCepat0PDP2': 19,
        'DanaCepat0PDP3': 20,
        'DanaCepat03': 21,
        'PinjamUang0PDP2': 22,
        'PinjamUang0PDP3': 23,
        'PinjamUang03': 24,
        'KtaKilat0PDP2': 25,
        'KtaKilat0PDP3': 26,
        'KtaKilat03': 27,
        'DanaCepat1PDP2': 28,
        'DanaCepat1PDP3': 29,
        'PinjamUang1PDP2': 30,
        'PinjamUang1PDP3': 31,
        'KtaKilat1PDP2': 32,
        'KtaKilat1PDP3': 33,
        'DanaCepat13': 36,
        'PinjamUang13': 37,
        'KtaKilat13': 38,
        'DanaCepat12': 39,
        'PinjamUang12': 40,
        'KtaKilat12': 41,
        'DanaCepat02': 42,
        'PinjamUang02': 43,
        'KtaKilat02': 44,
        'IKIDana01': 100,
        'IKIDana00': 101,
        'IKIDana0PDP1': 102,
        'IKIDana11': 103,
        'IKIDana10': 104,
        'IKIDana1PDP1': 105,
        'IKIDana0PDP2': 106,
        'IKIDana0PDP3': 107,
        'IKIDana03': 108,
        'IKIDana1PDP2': 109,
        'IKIDana1PDP3': 110,
        'IKIDana13': 111,
        'IKIDana12': 112,
        'IKIDana02': 113,
    }
    current_page += 1
    with db.atomic() as transaction:
        while True:
            bill_service = BillService()
            #?????????????????????4????????????bill_sub.origin_due_at
            ivr_action = bill_service.ivr_pages(
                page=current_page,
                page_size=500,
                start_time=utc_datetime(str(start)),
                end_time=utc_datetime(str(end)))
            result = ivr_action['result']
            page_size = int(ivr_action.get('page_size', 0))
            total_page = int(ivr_action.get('total_page', 0))

            insert_args = []
            for a in result:
                due_at = no_utc_datetime(a['due_at'])
                days = (due_at.date() - now).days
                if days == 2:
                    continue
                if days > 0:
                    time = str(days)
                else:
                    # ?????????????????????????????????????????????????????????????????????????????????
                    time = str(days).replace('-', 'PDP')

                #su  ????????????????????????
                key = a['app_name'] + str(a['su']) + time
                group = auto_ivr.get(key)

                user_id = a['user_id']
                try:
                    user_resp = (AccountService()
                                 .get_user(path_params={'user_id': user_id}))
                    if str(user_resp['mobile_no']) == str(a['user_mobile_no']):
                        numbers = a['user_mobile_no']
                    else:
                        numbers = (a['user_mobile_no'] +
                                   ',' + user_resp.get('mobile_no'))
                except:
                    logging.error('Request Account Service Error.')
                    numbers = a['user_mobile_no']

                insert_args.append({
                    'application_id': a['id'],
                    'numbers': numbers,
                    'group': group,
                    'user_id': user_id})

            AutoIVR.insert_many(insert_args).execute()
            if current_page == 1:
                IVRActionLog.create(total_page=total_page,
                                    proc_date=now,
                                    page_size=page_size,
                                    current_page=current_page)
                # ??????????????????????????????create???????????????????????????
                item = IVRActionLog.get(IVRActionLog.proc_date == now)
            else:
                item.current_page = current_page
                item.page_size = page_size
                item.total_page = total_page
                item.save()
            transaction.commit()
            current_page += 1
            if current_page > int(total_page):
                break
    # try:
    #     ivr_t2_test()
    # except Exception as e:
    #     logging.error("ivr_test_error:%s"%str(e))

    if sys_config and sys_config.value:
        try:
            classfiy_dpd_ptp_apps()
        except Exception as e:
            logging.error("dpd1-3_test_error:%s"%str(e))



# t-2???ivr????????????
def ivr_t2_test():
    t2_groups = [39, 40, 41, 42, 43, 44]
    ivr_test_proportion = 0.2
    sys_config = (SystemConfig.select()
                  .where(SystemConfig.key == 'IVR_TEST_PROPORTION')
                  .first())
    if sys_config and sys_config.value:
        ivr_test_proportion = float(sys_config.value)
    # ????????????t-2??????
    t2_ivrs = (AutoIVR.select()
               .where(AutoIVR.group << t2_groups,
                      AutoIVR.status == AutoIVRStatus.AVAILABLE.value))
    t2_dict = defaultdict(list)
    # ??????group????????????????????????
    for ivr in t2_ivrs:
        t2_dict[ivr.group].append(ivr.id)
    test_ivr_ids = []
    for group, ivr_ids in t2_dict.items():
        number = ceil(len(ivr_ids) * ivr_test_proportion)
        test_ivr_ids += ivr_ids[:number]
    if not test_ivr_ids:
        return
    # ??????ivr??????
    q = (AutoIVR.update(status=AutoIVRStatus.SUCCESS.value)
         .where(AutoIVR.group << t2_groups,
                AutoIVR.id.not_in(test_ivr_ids))
         .execute())

# ?????????bomber??????p??????
def classfiy_dpd_ptp_apps():
    dpd_group = AutoIVR.dpd_groups()
    dpd1_3_ivr_pro = 0.2
    sys_config = (SystemConfig.select()
                  .where(SystemConfig.key == 'DPD1-3_IVR_TEST')
                  .first())
    if sys_config and sys_config.value:
        dpd1_3_ivr_pro = float(sys_config.value)
    # ????????????????????????p??????
    apps = (ApplicationR.select(ApplicationR.external_id)
            .where(ApplicationR.overdue_days < 4,
                   ApplicationR.status != ApplicationStatus.REPAID.value,
                   ApplicationR.promised_date >= date.today(),
                   ApplicationR.promised_date.is_null(False)))
    apps_ids = [a.external_id for a in apps]
    # ??????ivr??????p??????
    if apps_ids:
        d = (AutoIVR.delete()
             .where(AutoIVR.application_id.in_(apps_ids),
                    AutoIVR.group.in_(dpd_group))
             .execute())
    # ??????dpd1-3??????
    ivrs = (AutoIVR.select().where(AutoIVR.group.in_(dpd_group)))
    ivrs_dict = defaultdict(list)
    for ivr in ivrs:
        ivrs_dict[ivr.group].append(ivr.id)
    test_ivrs = []
    for group, ivr_ids in ivrs_dict.items():
        number = ceil(len(ivr_ids) * dpd1_3_ivr_pro)
        test_ivrs += ivr_ids[:number]
    if not test_ivrs:
        return
    # ?????????????????????????????????
    q = (AutoIVR.update(status=AutoIVRStatus.SUCCESS.value)
         .where(AutoIVR.group.in_(dpd_group),
                AutoIVR.id.not_in(test_ivrs))
         .execute())



# APP ??????????????????
@action(MessageAction.APP_MERGE)
@deprecated(version='1.0', reason='This function will be removed soon')
def app_merge(payload, msg_id):

    # ???DPD??????4???????????????bomber
    sql = """
            select *
            from (
            select a.id as id
            from dashboard.application as a
            inner join repayment.bill2 as b on b.external_id = a.id
            where not exists (
                  select 1
                  from battlefront.user_login_log as u
                  where u.created_at > '2018-08-16'
                  and u.user_id = a.user_id
              )
              and a.app = 'DanaCepat'
              and a.is_first_loan = 1
              and a.apply_at < '2018-08-23 20:50:00'
              and b.overdue_days between 1 and 3
              and b.status != 2) result
            where not exists (
               select 1
               from bomber.application as a
               where a.cycle = 1
               and a.status = 4
               and a.id = result.id
            )
          """
    cursor = readonly_db.get_cursor()
    cursor.execute(sql)
    new_data = cursor.fetchall()
    cursor.close()
    if new_data:
        bomber = [103, 104]
        for d in new_data:
            app_id = {'id': d[0]}
            application_overdue(app_id, None)

            # ??????????????????????????????????????????
            (Application
             .update(status=ApplicationStatus.AB_TEST.value,
                     latest_bomber=random.choice(bomber),
                     ptp_bomber=None
                     )
             .where(Application.id == d[0])
             ).execute()
        logging.warning('add new app success')

    # ???????????????????????????ptp?????????????????????????????????
    ptp = date.today() - timedelta(days=1)
    del_sql = """
              select a.id
              from bomber.application as a
              where exists(
               select 1
               from battlefront.user_login_log as u
               where u.created_at > '2018-08-16'
               and u.user_id = a.user_id
               )
               and a.cycle = 1
               and a.status = 4
               and (a.promised_date is null or a.promised_date < "%s")
              """ % ptp
    cursor = readonly_db.get_cursor()
    cursor.execute(del_sql)
    del_date = cursor.fetchall()
    cursor.close()
    if del_date:
        return
    ids = list()
    for d in del_date:
        ids.append(d[0])
    (Application
     .update(status=ApplicationStatus.UNCLAIMED.value,
             latest_bomber=None)
     .where(Application.id << ids)).execute()


@action(MessageAction.APPLICATION_BOMBER)
def application_overdue(payload, msg_id):

    application_id = payload['id']
    sub_bill_id = payload['bill_sub_id']
    local_app = (Application.select()
                 .where(Application.external_id == application_id)
                 .order_by(Application.finished_at)
                 .first())
    # ?????????????????????????????????
    if local_app and local_app.type != ApplicationType.CASH_LOAN_STAGING.value:
        logging.info('application %s overdue, already exists', application_id)
        add_contact(local_app)
        return
    # ???????????????,???????????????????????????
    if local_app and local_app.type == ApplicationType.CASH_LOAN_STAGING.value:
        overdue_bill = (OverdueBillR.select()
                        .where(OverdueBillR.sub_bill_id == sub_bill_id,
                               OverdueBillR.external_id == application_id))
        if overdue_bill.exists():
            logging.info(
                'application %s,sub_bill_id %s overdue, already exists' %
                (application_id, sub_bill_id))
            return

    try:
        sub_bill = BillService().sub_bill_list(bill_sub_ids=[sub_bill_id])
        sub_bill = sub_bill[0]
    except Exception:
        logging.error('application %s overdue, get sub_bill info failed:'
                      'Request To repayment Error', application_id)
        return

    if sub_bill['status'] == 2:
        logging.error('application %s overdue, but bills already cleared',
                      application_id)
        return

    overdue_days = sub_bill.get('overdue_days', 0)
    if overdue_days == 0:
        logging.info('application {} no overdue'
                           .format(str(application_id)))
        return

    gold_eye = GoldenEye().get('/applications/%s' % application_id)
    if not gold_eye.ok:
        logging.error('get application %s failed: Request to GoldenEye.',
                      application_id)
        return
    gold_app = gold_eye.json().get('data')
    user_id = gold_app['user_id']

    apply_history = Dashboard().get('/users/%s/apply-history' % user_id)
    if not apply_history.ok:
        logging.error('get user %s apply history failed: Request '
                      'to Dashboard Failed.', user_id)
        return
    history = apply_history.json().get('data')
    loan_success_times = len([1 for i in history
                              if i['status'] in [80, 90, 100, 70] and
                              i['id'] != gold_app['id']])

    id = application_id
    type = ApplicationType.CASH_LOAN.value
    bill_id = sub_bill.get("bill_id")
    amount = sub_bill.get("amount")
    amount_net = sub_bill.get('amount_net')
    interest_rate = sub_bill.get('interest_rate')
    overdue_days = sub_bill.get('overdue_days')
    origin_due_at = sub_bill.get('origin_due_at')
    sub_overdue_bill = {
        "collection_id": id,
        "bill_id": bill_id,
        "sub_bill_id": sub_bill_id,
        "periods": sub_bill.get("periods"),
        "overdue_days": overdue_days,
        "origin_due_at": origin_due_at,
        "amount": amount,
        "amount_net": amount_net,
        "interest_rate": interest_rate,
        "external_id": application_id
    }
    # ??????????????????????????????id
    if sub_bill['category'] == ApplicationType.CASH_LOAN_STAGING.value:
        if local_app and local_app.status != ApplicationStatus.REPAID.value:
            sub_overdue_bill["collection_id"] = local_app.id
            local_app.amount += amount
            local_app.amount_net += amount_net
            local_app.save()
            new_overdue = OverdueBill.create(**sub_overdue_bill)
            logging.info(
                "application %s,sub_bill_id:%s overdue created" %
                (application_id, sub_bill_id))
            return
        else:
            id = idg()
            type = ApplicationType.CASH_LOAN_STAGING.value
            sub_overdue_bill["collection_id"] = id

    ptp_info = BombingHistory.filter(BombingHistory.application == id).first()

    promised_amount = ptp_info and ptp_info.promised_amount
    promised_date = ptp_info and ptp_info.promised_date

    application = Application.create(
        id=id,
        user_id=gold_app['user_id'],
        user_mobile_no=gold_app['user_mobile_no'],
        user_name=gold_app['id_name'],
        app=gold_app['app'],
        device_no=gold_app['device_no'],
        contact=json.dumps(gold_app.get('contact')),
        apply_at=gold_app.get('apply_date'),

        id_ektp=gold_app.get('id_ektp'),
        birth_date=birth_dt_ktp(gold_app.get('id_ektp')),
        gender=gender_ktpnum(gold_app.get('id_ektp')),

        profile_province=(gold_app.get('profile_province') or {}).get('name'),
        profile_city=(gold_app.get('profile_city') or {}).get('name'),
        profile_district=(gold_app.get('profile_district') or {}).get('name'),
        profile_residence_time=gold_app.get('profile_residence_time'),
        profile_residence_type=gold_app.get('profile_residence_type'),
        profile_address=gold_app.get('profile_address'),
        profile_education=gold_app.get('profile_education'),
        profile_college=(gold_app.get('profile_college') or {}).get('name'),

        job_name=gold_app.get('job_name'),
        job_tel=gold_app.get('job_tel'),
        job_bpjs=gold_app.get('job_bpjs'),
        job_user_email=gold_app.get('job_user_email'),
        job_type=gold_app.get('job_type'),
        job_industry=gold_app.get('job_industry'),
        job_department=gold_app.get('job_department'),
        job_province=(gold_app.get('job_province') or {}).get('name'),
        job_city=(gold_app.get('job_city') or {}).get('name'),
        job_district=(gold_app.get('job_district') or {}).get('name'),
        job_address=gold_app.get('job_address'),

        amount=amount,
        amount_net=amount_net,
        interest_rate=interest_rate,
        # late_fee_rate=bill.get('late_fee_rate'),
        # late_fee_initial=late_fee_initial,
        # late_fee=late_fee,
        # interest=interest,
        term=gold_app.get('term'),
        origin_due_at=origin_due_at,
        # due_at=bill.get('due_at'),
        overdue_days=overdue_days,

        repay_at=sub_bill.get('repay_at'),
        # principal_paid=principal_paid,
        # late_fee_paid=late_fee_paid,
        # repaid=repaid,
        # unpaid=unpaid,

        loan_success_times=loan_success_times,
        arrived_at=datetime.now(),
        follow_up_date=datetime.now(),

        promised_amount=promised_amount,
        promised_date=promised_date,
        external_id=application_id,
        type=type,
        bill_id=bill_id,
        dpd1_entry=datetime.now()
    )

    new_overdue = OverdueBill.create(**sub_overdue_bill)

    logging.info('overdue application %s created', application_id)

    # new overdue application equals to 'escalate from 0 to 1'
    Escalation.create(
        application=id,
        type=EscalationType.AUTOMATIC.value,
        status=ApprovalStatus.APPROVED.value,
        current_cycle=0,
        escalate_to=1,
    )
    add_contact(application)


def add_contact(application):

    logging.info('start add contact for application: %s', application.id)

    # ?????????????????????
    contacts = Contact.filter(
        Contact.user_id == application.user_id,
    )
    existing_numbers = {contact.number for contact in contacts}

    insert_contacts = list()

    mon_insert_contact = {}
    # applicant
    user_mobile_no = number_strip(application.user_mobile_no)
    if user_mobile_no and user_mobile_no not in existing_numbers:
        insert_contacts.append({
            'user_id': application.user_id,
            'name': application.user_name,
            'number': user_mobile_no,
            'relationship': Relationship.APPLICANT.value,
            'source': 'apply info',
            'real_relationship': Relationship.APPLICANT.value
        })
        existing_numbers.add(number_strip(application.user_mobile_no))

    extra_phone = GoldenEye().get(
        '/users/%s/extra-phone' % application.user_id
    )
    if not extra_phone.ok:
        extra_phone = []
        logging.error('get user %s extra contacts failed',
                      application.user_id)
    else:
        extra_phone = extra_phone.json()['data']

    if extra_phone:
        for i in extra_phone:
            number = number_strip(i['number'])[:64]
            if not number:
                continue
            if number in existing_numbers:
                continue
            insert_contacts.append({
                'user_id': application.user_id,
                'name': application.user_name,
                'number': number,
                'relationship': Relationship.APPLICANT.value,
                'source': 'extra phone',
                'real_relationship': Relationship.APPLICANT.value
            })
            key = user_mobile_no, number, ContactType.A_EXTRA_PHONE.value
            mon_insert_contact[key] = 1, 0, application.user_name
        existing_numbers.add(number)

    # family
    # ec contact
    ec_contact = []
    contact = json.loads(application.contact or '[]')
    for i in contact:
        if (number_strip(i['mobile_no']) not in existing_numbers and
                number_strip(i['mobile_no'])):
            ec_contact.append({
                'user_id': application.user_id,
                'name': i['name'],
                'number': number_strip(i['mobile_no']),
                'relationship': Relationship.FAMILY.value,
                'sub_relation': SubRelation.EC.value,
                'source': FamilyContactType.EC.value,
                'real_relationship': Relationship.FAMILY.value
            })
            key = (user_mobile_no,
                   number_strip(i['mobile_no']),
                   ContactType.F_EC.value)
            mon_insert_contact[key] = 1, 0, i['name']
            existing_numbers.add(number_strip(i['mobile_no']))
        if i['type'] != 1:
            continue
        if (number_strip(i['tel_no']) not in existing_numbers and
                number_strip(i['tel_no'])):
            ec_contact.append({
                'user_id': application.user_id,
                'name': i['name'],
                'number': number_strip(i['tel_no']),
                'relationship': Relationship.FAMILY.value,
                'sub_relation': SubRelation.EC.value,
                'source': FamilyContactType.EC.value,
                'real_relationship': Relationship.FAMILY.value
            })
            key = (user_mobile_no,
                   number_strip(i['tel_no']),
                   ContactType.F_EC.value)
            mon_insert_contact[key] = 1, 0, i['name']
            existing_numbers.add(number_strip(i['tel_no']))

    if ec_contact:
        Contact.insert_many(ec_contact).execute()

    # company
    if all((application.job_tel,
            number_strip(application.job_tel),
            number_strip(application.job_tel) not in existing_numbers)):
        insert_contacts.append({
            'user_id': application.user_id,
            'name': None,
            'number': number_strip(application.job_tel),
            'relationship': Relationship.COMPANY.value,
            'source': 'basic info job_tel',
            'real_relationship': Relationship.COMPANY.value
        })
        key = (user_mobile_no,
               number_strip(application.job_tel),
               ContactType.C_BASIC_INFO_JOB_TEL.value)
        mon_insert_contact[key] = 1, 0, None
        existing_numbers.add(number_strip(application.job_tel))

    # suggested

    sms_contacts = GoldenEye().get(
        '/applications/%s/sms-contacts' % application.external_id
    )
    if not sms_contacts.ok:
        sms_contacts = []
        logging.info('get user %s sms contacts failed', application.external_id)
    else:
        sms_contacts = sms_contacts.json()['data']

    if sms_contacts:
        for i in sms_contacts:
            number = number_strip(i['number'])[:64]
            if not number:
                continue
            if number in existing_numbers:
                continue
            insert_contacts.append({
                'user_id': application.user_id,
                'name': i['name'][:128],
                'number': number,
                'relationship': Relationship.SUGGESTED.value,
                'source': 'sms contacts',
                'real_relationship': Relationship.SUGGESTED.value
            })
            key = (user_mobile_no,
                   number,
                   ContactType.S_SMS_CONTACTS.value)
            mon_insert_contact[key] = 1, 0, i['name'][:128]
            existing_numbers.add(number)

    if insert_contacts:
        Contact.insert_many(insert_contacts).execute()

    cf = GoldenEye().get(
        '/applications/%s/call/frequency' % application.external_id
    )
    if not cf.ok:
        call_frequency = []
        logging.error('get application %s call frequency error',
                      application.external_id)
    else:
        call_frequency = cf.json()['data']

    # ??????????????????????????????
    insert_contacts = []
    fm = GoldenEye().get(
        '/applications/%s/contact/family-member' % application.external_id
    )
    if not fm.ok:
        family = []
        logging.error('get application %s family-member info error',
                      application.external_id)
    else:
        family = fm.json()['data']
    if family:
        for i in family:
            if not (i.get('number')):
                logging.info('family member %s' % str(i))
                continue
            number = number_strip(i['number'])[:64]
            if not number:
                continue
            if number in existing_numbers:
                continue
            logging.info('family members: %s' % str(i))
            insert_contacts.append({
                'user_id': application.user_id,
                'name': i['name'][:128],
                'number': number,
                'relationship': Relationship.FAMILY.value,
                'source': FamilyContactType.CALLEC.value,
                'total_count': i.get('total_count', 1),
                'total_duration': i.get('total_duration', 0),
                'real_relationship': Relationship.FAMILY.value
            })
            key = user_mobile_no, number, ContactType.F_CALL_EC.value
            mon_insert_contact[key] = (i.get('total_count', 1),
                                       i.get('total_duration', 0),
                                       i['name'][:128])
            existing_numbers.add(number)

    mon_update_contact = {}
    if call_frequency:
        with db.atomic():
            count = 1
            for i in call_frequency:
                number = number_strip(i['number'])[:64]
                if not number:
                    continue
                if number in existing_numbers:
                    (Contact
                     .update(total_count=i['total_count'],
                             total_duration=i['total_duration'])
                     .where(Contact.number == number,
                            Contact.user_id == application.user_id))
                    key = user_mobile_no, number
                    mon_update_contact[key] = (i['total_count'],
                                               i['total_duration'])
                    continue

                # ????????????????????????????????????family member
                if count < 6:
                    insert_contacts.append({
                        'user_id': application.user_id,
                        'name': i['name'][:128],
                        'number': number,
                        'relationship': Relationship.FAMILY.value,
                        'total_count': i['total_count'],
                        'total_duration': i['total_duration'],
                        'source': FamilyContactType.CALLTOP5.value,
                        'real_relationship': Relationship.FAMILY.value
                    })
                    count += 1
                    key = user_mobile_no, number, ContactType.F_CALL_TOP5.value
                    mon_insert_contact[key] = (i['total_count'],
                                               i['total_duration'],
                                               i['name'][:128])
                else:
                    insert_contacts.append({
                        'user_id': application.user_id,
                        'name': i['name'][:128],
                        'number': number,
                        'relationship': Relationship.SUGGESTED.value,
                        'total_count': i['total_count'],
                        'total_duration': i['total_duration'],
                        'source': 'call frequency',
                        'real_relationship': Relationship.SUGGESTED.value
                    })
                    key = (user_mobile_no,
                           number,
                           ContactType.S_CALL_FREQUENCY.value)
                    mon_insert_contact[key] = (i['total_count'],
                                               i['total_duration'],
                                               i['name'][:128])

                existing_numbers.add(number)
            if insert_contacts:
                Contact.insert_many(insert_contacts).execute()

    # ?????????????????????????????????
    next_apply_list = (AccountService().add_contact(application.user_id))

    for next_apply in next_apply_list:
        number = number_strip(str(next_apply))[:64]
        if number and number not in existing_numbers:
            Contact.create(
                user_id=application.user_id,
                name=application.user_name,
                number=number,
                relationship=Relationship.SUGGESTED.value,
                source='online profile phone',
                real_relationship=Relationship.SUGGESTED.value
            )
            key = (user_mobile_no,
                   number,
                   ContactType.S_ONLINE_PROFILE_PHONE.value)
            mon_insert_contact[key] = 1, 0, application.user_name
            existing_numbers.add(number)

    # ????????????????????????????????????????????????
    next_applicant = GoldenEye().get(
        '/bomber/%s/dual_contact' % application.user_id
    )
    if not next_applicant.ok:
        next_applicant = []
        logging.error('get user %s dual_contact contacts failed'
                      % application.user_id)
    else:
        next_applicant = next_applicant.json()['data']

    if next_applicant:
        for i in next_applicant:
            number = number_strip(str(i))[:64]
            if number and number not in existing_numbers:
                Contact.create(
                    user_id=application.user_id,
                    name=application.user_name,
                    number=number,
                    relationship=Relationship.APPLICANT.value,
                    source='apply info',
                    real_relationship=Relationship.APPLICANT.value
                )
                key = user_mobile_no, number, ContactType.A_APPLY_INFO.value
                mon_insert_contact[key] = 1, 0, application.user_name
                existing_numbers.add(number)
            logging.info('get user %s dual_contact contacts success' %
                         application.user_id)

    # add new contact
    # ?????????ktp????????????????????????????????????
    numbers = []
    try:
        numbers = (AccountService()
                   .ktp_number(path_params={'user_id': application.user_id}))
    except Exception as e:
        logging.info('request ktp numbers failed %s' % str(e))

    for n in numbers:
        number = number_strip(str(n))[:64]
        if number and number not in existing_numbers:
            Contact.create(
                user_id=application.user_id,
                name=application.user_name,
                number=number,
                relationship=Relationship.APPLICANT.value,
                source='ktp number',
                real_relationship=Relationship.APPLICANT.value
            )
            key = (user_mobile_no,
                   number,
                   ContactType.A_KTP_NUMBER.value)
            mon_insert_contact[key] = 1, 0, application.user_name
            existing_numbers.add(number)
        logging.info('get user %s dual_contact contacts success'
                     % application.user_id)

    # ???contact??????is_family???true????????????ec
    try:
        ecs = GoldenEye().get(
            '/applications/%s/contact/ec' % application.external_id
        )
    except Exception as e:
        logging.info('request ec-member error: %s' % str(e))
    try:
        if not ecs.ok:
            ec = []
            logging.info('get application %s ec-member info error',
                         application.external_id)
        else:
            ec = ecs.json()['data']

        if ec:
            for e in ec:
                number = number_strip(e['numbers'])[:64]
                if not number:
                    continue
                if number not in existing_numbers:
                    Contact.create(
                        user_id=application.user_id,
                        name=e['name'][:128],
                        number=number,
                        relationship=Relationship.FAMILY.value,
                        source=FamilyContactType.CONTACTEC.value,
                        real_relationship=Relationship.FAMILY.value
                    )
                    key = (user_mobile_no,
                           number,
                           ContactType.F_CONTACT_EC.value)
                    mon_insert_contact[key] = 1, 0, e['name'][:128]
                    existing_numbers.add(number)
    except Exception as e:
        logging.info('add ec_member error:%s' % str(e))

    # ???contact???is_me?????????true??????????????????
    try:
        mn = GoldenEye().get(
            '/applications/%s/contact/my_number' % application.external_id
        )
    except Exception as e:
        logging.info('request my_number error: %s' % str(e))
    try:
        if not mn.ok:
            my = []
            logging.info('get application %s my_number info error',
                         application.external_id)
        else:
            my = mn.json()['data']

        if my:
            for m in my:
                number = number_strip(m)[:64]
                if not number:
                    continue
                if number not in existing_numbers:
                    Contact.create(
                        user_id=application.user_id,
                        name=my[m][:128],
                        number=number,
                        relationship=Relationship.SUGGESTED.value,
                        source='my number',
                        real_relationship=Relationship.SUGGESTED.value
                    )
                    key = user_mobile_no, number, ContactType.S_MY_NUMBER.value
                    mon_insert_contact[key] = 1, 0, my[m][:128]
                    existing_numbers.add(number)
    except Exception as e:
        logging.info('add my_member error:%s' % str(e))

    # ??????company?????????
    try:
        cn = GoldenEye().get(
            '/applications/%s/contact/company-number' % application.external_id
        )
    except Exception as e:
        logging.info('request company-number error: %s' % str(e))
    try:
        if not cn.ok:
            cn = []
            logging.info('get application %s company_number info error',
                         application.external_id)
        else:
            cn = cn.json()['data']

        if cn:
            for c in cn:
                number = c
                if not number:
                    continue
                if number not in existing_numbers:
                    Contact.create(
                        user_id=application.user_id,
                        name=cn[c][:128],
                        number=number,
                        relationship=Relationship.COMPANY.value,
                        source='company',
                        real_relationship=Relationship.COMPANY.value
                    )
                    key = user_mobile_no, number, ContactType.C_COMPANY.value
                    mon_insert_contact[key] = 1, 0, cn[c][:128]
                    existing_numbers.add(number)
    except Exception as e:
        logging.info('add company_member error:%s' % str(e))

    # ???????????????????????????????????????sim?????????????????????applicant???
    try:
        ol = (AccountService()
              .other_login_contact(userId=application.user_id))
    except Exception as e:
        logging.error('request other_login error: %s' % e)
        ol = {}

    try:
        for o in ol:
            number = number_strip(o)
            if not number:
                continue
            if number not in existing_numbers:
                Contact.create(
                    user_id=application.user_id,
                    name=ol[o][:128],
                    number=number,
                    relationship=Relationship.SUGGESTED.value,
                    source='other_login',
                    real_relationship=Relationship.SUGGESTED.value
                )
                key = (user_mobile_no,
                       number,
                       ContactType.S_OTHER_LOGIN.value)
                mon_insert_contact[key] = 1, 0, ol[o][:128]
    except Exception as e:
        logging.error('add other_login number error:%s' % e)

    logging.info('add contact for application %s finished', application.id)
    if mon_insert_contact or mon_update_contact:
        send_to_default_q(MessageAction.IMPORT_CONTACT_TO_MON,
                          {
                              'user_mobile_no': user_mobile_no,
                              'insert_contact': str(mon_insert_contact),
                              'update_contact': str(mon_update_contact),
                              'user_id': application.user_id,
                              'name': application.user_name
                          })


@action(MessageAction.IMPORT_CONTACT_TO_MON)
def import_contact_to_mon(payload, msg_id):
    user_mobile_no = payload['user_mobile_no']
    insert_contact = eval(payload['insert_contact'])
    update_contact = eval(payload['update_contact'])
    user_id = payload['user_id']
    name = payload['name']

    if not (insert_contact or update_contact or user_mobile_no):
        logging.error("Invalid params")
        drop_duplicated_contact({'numbers': [user_mobile_no]}, None)
        send_to_default_q(MessageAction.CONTACT_FROM_TOTAL, {
            'number': user_mobile_no,
            'user_id': user_id
        })
        return

    contacts = TotalContact.objects(src_number=user_mobile_no, is_calc=False)
    insert_list = []
    for c in contacts:
        key = (user_mobile_no, c.dest_number, c.source)
        if key in insert_contact:
            insert_contact.pop(key)

    for (sn, dn, s), (tc, td, na) in insert_contact.items():
        insert_list.append({
            'src_number': sn,
            'src_name': name,
            'dest_number': dn,
            'dest_name': na,
            'source': s,
            'total_count': tc,
            'total_duration': td
        })

    if insert_list:
        insert_count = len((TotalContact
                            .objects
                            .insert([TotalContact(**dct)
                                     for dct in insert_list])))
        logging.info("insert success %s", insert_count)

    update_count = 0
    for (sn, dn), (tc, td) in update_contact.items():
        result = (TotalContact
                  .objects(src_number=sn, dest_number=dn, is_calc=False)
                  .update(total_count=tc, total_duration=td))
        if result:
            update_count += 1
    logging.info("update success %s", update_count)

    drop_duplicated_contact({'numbers': [user_mobile_no]}, None)
    send_to_default_q(MessageAction.CONTACT_FROM_TOTAL, {
        'number': user_mobile_no,
        'user_id': user_id
    })


@action(MessageAction.DROP_DUPLICATED_CONTACT)
def drop_duplicated_contact(payload, msg_id):
    """
    total_count,total_duration?????????,???total_count, ???total_duration

    :param payload:
    :param msg_id:
    :return:
    """
    numbers = payload.get('numbers', [])
    if not numbers:
        logging.error("no numbers should drop")

    query = (TotalContact
             .objects(Q(src_number__in=numbers) | Q(dest_number__in=numbers)))

    contact_list = defaultdict(list)
    delete_list = []
    insert_list = []
    for c in query:
        if c.src_number == c.dest_number:
            delete_list.append(c.id)

        key = c.src_number, c.dest_number, c.source
        contact_list[key].append({
            'id': c.id,
            'src_number': c.src_number,
            'dest_number': c.dest_number,
            'total_count': c.total_count,
            'total_duration': c.total_duration,
            'is_calc': c.is_calc,
            'source': c.source,
            'src_name': c.src_name,
            'dest_name': c.dest_name
        })

    contact_list2 = deepcopy(contact_list)
    for key, info in contact_list.items():
        _info = sorted(info,
                       key=lambda x: (not x['is_calc'],
                                      x['total_count'],
                                      x['total_duration']),
                       reverse=True)
        rs = _info[0]
        if not rs['is_calc']:
            contact_list2[(key[1], key[0], key[2])].append({
                'src_number': rs['dest_number'],
                'dest_number': rs['src_number'],
                'total_count': rs['total_count'],
                'total_duration': rs['total_duration'],
                'is_calc': True,
                'source': rs['source'],
                'id': '',
                'src_name': rs['dest_name'],
                'dest_name': rs['src_name']
            })
            delete_ids = [i['id'] for i in _info[1:] if i['id']]
            delete_list.extend(delete_ids)

    for key, info in contact_list2.items():
        _info = sorted(info,
                       key=lambda x: (not x['is_calc'],
                                      x['total_count'],
                                      x['total_duration']),
                       reverse=True)
        rs = _info[0]
        # ???????????????????????????????????????????????????
        if not rs['is_calc']:
            continue
        if not rs['id']:
            rs.pop('id')
            insert_list.append(rs)

        delete_ids = [i['id'] for i in _info[1:] if i['id']]
        delete_list.extend(delete_ids)

    if delete_list:
        delete_count = TotalContact.objects(id__in=delete_list).delete()
        logging.info("numbers %s: delete success %s", numbers, delete_count)

    if insert_list:
        insert_count = len((TotalContact
                            .objects
                            .insert([TotalContact(**dct)
                                     for dct in insert_list])))
        logging.info("numbers %s: insert success %s", numbers, insert_count)


def get_contact_from_mongo(number):
    if not number:
        return []

    query = (TotalContact
             .objects(src_number=number,
                      source__in=TotalContact.available())
             .order_by('source'))
    lst = []
    for c in query:
        relation = TotalContact.relationship(c.source)
        if relation == -1:
            continue
        source = TotalContact.str_source(c.source)
        if not source:
            continue
        lst.append({
            'related_number': c.dest_number,
            'source': source,
            'is_calc': c.is_calc,
            'total_count': c.total_count,
            'total_duration': c.total_duration,
            'relation': relation,
            'name': c.dest_name
        })
    return lst


@action(MessageAction.CONTACT_FROM_TOTAL)
def contact_from_total(payload, msg_id):
    number = payload.get('number')
    user_id = payload.get('user_id')
    if not (number and user_id):
        logging.error("Invalid params")
        return
    result = get_contact_from_mongo(number)
    if not result:
        logging.error("contact from mongo is none")
        return

    contacts = Contact.filter(Contact.user_id == user_id)
    existing_numbers = {contact.number for contact in contacts}
    contact_list = []

    for c in result:
        number = number_strip(c['related_number'])
        if number in existing_numbers:
            continue

        contact_list.append({
            'user_id': user_id,
            'name': c['name'],
            'number': number,
            'relationship': c['relation'],
            'source': c['source'],
            'total_duration': c['total_duration'],
            'total_count': c['total_count'],
            'real_relationship': c['relation']
        })
        existing_numbers.add(number)

    if contact_list:
        Contact.insert_many(contact_list).execute()


@action(MessageAction.BILL_REVOKE)
def bill_revoke(payload, msg_id):
    application_id = payload['external_id']
    if 'bill_sub_id' not in payload:
        bill_revoke_old(application_id)
        return
    # ?????????id
    sub_bill_id = payload['bill_sub_id']
    # java???????????????????????????
    partner_bill_id = payload['partner_bill_id']

    application = (Application
                   .filter(Application.external_id == application_id).first())

    if application.type == ApplicationType.CASH_LOAN_STAGING.value:
        # ?????????????????????????????????id
        application = (Application.select(Application)
                       .join(OverdueBill,JOIN_LEFT_OUTER,
                             on = Application.id == OverdueBill.collection_id)
                       .where(OverdueBill.external_id == application_id,
                              OverdueBill.sub_bill_id == sub_bill_id)
                       .first())
    if not application:
        logging.info('application %s paid, not found application',
                     application_id)
        return

    try:
        bill = BillService().sub_bill_list(bill_sub_ids = [sub_bill_id])
        bill = bill[0]
    except Exception:
        logging.error('application %s overdue, get bill info failed: '
                      'Request To Repayment Error', application_id)
        raise RuntimeError('Get repayment bills failed. {}'
                           .format(str(application.id)))

    if bill.get('overdue_days') > 0 and bill.get('status') != 2:

        Application.update(
            status=ApplicationStatus.UNCLAIMED.value
        ).where(Application.id == application.id).execute()
        # ???????????????
        overdue_bill = (OverdueBill
                        .filter(OverdueBill.external_id == application_id,
                                OverdueBill.sub_bill_id == sub_bill_id)
                        .first())
        if not overdue_bill:
            logging.info("not find overdue_bill,sub_bill_id:%s,appid:%s" %
                         (sub_bill_id, application_id))
            return
        if overdue_bill.status == ApplicationStatus.REPAID.value:
            overdue_bill.status = ApplicationStatus.UNCLAIMED.value
            overdue_bill.finished_at = None
            overdue_bill.save()
        # ???????????????????????????
        RepaymentLog.update(
            no_active = 1
        ).where(RepaymentLog.partner_bill_id == partner_bill_id,
                RepaymentLog.overdue_bill_id == overdue_bill.id).execute()

# ?????????????????????
def bill_revoke_old(external_id):
    application = (Application.select()
                   .where(Application.id == external_id)
                   .first())
    if not application:
        logging.info("not get application")
        return
    try:
        bill = BillService().bill_dict(
            application_id=external_id)
    except Exception:
        logging.error('application %s overdue, get bill info failed: '
                      'Request To Repayment Error', external_id)
        return
    if bill.get('overdue_days') >0 and bill.get("status") != 2:
        q = (Application
             .update(status=ApplicationStatus.UNCLAIMED.value,
                     repay_at=bill.get('repay_at'))
             .where(Application.id == external_id).execute())
        p = (OverdueBill.update(status=ApplicationStatus.UNCLAIMED.value)
             .where(OverdueBill.collection_id == external_id).execute())
    return


def check_key_not_none(payload, keys):
    for key in keys:
        if payload.get(key) is None:
            logging.error('Missing args {}'.format(str(key)))
            return False
    return True


# ??????
@action(MessageAction.BILL_PAID)
def bill_paid(payload, msg_id):
    # Don't use validator, it will throw exception
    validate = check_key_not_none(payload,
                                  ['external_id', 'late_fee_part',
                                   'principal_part', 'paid_at','bill_sub_id',
                                   'partner_bill_id'])
    if not validate:
        logging.error('payload key not fully pass in.')
        return

    external_id = payload['external_id']

    late_fee_part = Decimal(payload['late_fee_part'])
    principal_part = Decimal(payload['principal_part'])
    paid_at = payload['paid_at']
    partner_bill_id = payload['partner_bill_id']

    logging.debug('application %s paid principal part %s, paid late fee '
                  'part %s', external_id, principal_part, late_fee_part)

    application = (Application
                   .filter(Application.external_id == external_id)
                   .order_by(-Application.created_at)
                   .first())
    if not application:
        logging.info('application %s paid, not found application',external_id)
        return

    # ????????????
    sub_bill_id = payload['bill_sub_id']
    overdue_bill = (OverdueBillR.select()
                    .where(OverdueBillR.collection_id == application.id,
                           OverdueBillR.sub_bill_id == sub_bill_id)
                    .first())
    if (application.type == ApplicationType.CASH_LOAN_STAGING.value
            and not overdue_bill):
        logging.info("bill sub not in bomber %s",sub_bill_id)
        return
    with db.atomic():
        repay_at = str_no_utc_datetime(payload['latest_repay_at'])

        Application.update(
            repay_at=repay_at
        ).where(Application.id == application.id).execute()

        # ??????????????????????????? ???????????? is_bombed = True

        RepaymentLog.create(
            application=application.id,
            is_bombed=True,
            current_bomber=application.latest_bomber_id,
            cycle=application.cycle,
            principal_part=principal_part,
            late_fee_part=late_fee_part,
            repay_at=paid_at,
            ptp_bomber=application.ptp_bomber,
            latest_call=application.latest_call,
            periods=overdue_bill.periods if overdue_bill else None,
            overdue_bill_id=overdue_bill.id if overdue_bill else None,
            partner_bill_id=partner_bill_id
        )

        # ???????????? ?????? ????????????????????????
        phone_status = PhoneStatus.CONNECTED.value
        real_relationship = RealRelationship.user_values()
        commit = CallActionCommit.NO.value
        number = (CallActions.select(CallActions.number)
                  .where(CallActions.phone_status == phone_status,
                         CallActions.real_relationship << real_relationship,
                         CallActions.commit == commit,
                         CallActions.application == application.id)
                  .order_by(-CallActions.created_at)
                  .first())
        if number:
            (Contact.update(call_priority=PriorityStatus.REPAY.value)
             .where(Contact.user_id == application.user_id,
                    Contact.call_priority == PriorityStatus.LAST.value)
             ).execute()

            (Contact.update(call_priority=PriorityStatus.LAST.value)
             .where(Contact.user_id == application.user_id,
                    Contact.number == number.number)
             ).execute()

        if not application.latest_bomber_id:
            return

        Inbox.create(
            title='application %s,sub_bill_id %s repaid' % (
                    application.external_id, sub_bill_id),
            content='application %s,sub_bill_id %s repaid' % (
                     application.external_id, sub_bill_id),
            receiver=(application.latest_bomber_id or
                      application.last_bomber_id),
            category=InboxCategory.REPAID.value,
        )


@action(MessageAction.BILL_RELIEF)
def bill_relief(payload, msg_id):
    """?????????"""
    bill = payload['head_bill']

    repay_at = str_no_utc_datetime(bill['latest_repay_at'])
    updated_row = Application.update(
        repay_at=repay_at,
    ).where(Application.id == bill['external_id']).execute()

    logging.info('application %s bill relief done', bill['external_id'])
    return updated_row


# ???????????????
@action(MessageAction.BILL_CLEARED)
@action(MessageAction.BILL_CLEARED_BEFORE_CONFIRM)
def bill_cleared(payload, msg_id):
    """
    BILL_CLEARED_BEFORE_CONFIRM??????bomber???????????????,MST?????????????????????????????????
    ????????????????????????????????????
    """
    external_id = payload.get('external_id')
    sub_bill_id = payload.get('bill_sub_id')
    if not external_id:
        logging.warning('payload has no external_id. {}'.format(str(payload)))
        return

    # ?????????????????????????????????ivr
    AutoIVR.update(
        status=AutoIVRStatus.REPAID.value
    ).where(AutoIVR.application_id == external_id).execute()

    try:
        bill = BillService().sub_bill_list(bill_sub_ids=[sub_bill_id])
        bill = bill[0]
    except Exception:
        logging.error('get bill info failed: '
                      'Request To Repayment Error', external_id)
        return
    application = Application.filter(
        Application.external_id == external_id,
        Application.status << [ApplicationStatus.PROCESSING.value,
                               ApplicationStatus.UNCLAIMED.value,
                               ApplicationStatus.BAD_DEBT.value,
                               ApplicationStatus.AB_TEST.value]
    ).first()
    if not application:
        logging.info('application %s repay clear, not found bomber record',
                     external_id)
        return

    with db.atomic():
        # ????????????????????????????????????
        sub_bill_update = (OverdueBill.update(
                           status = ApplicationStatus.REPAID.value,
                           finished_at = datetime.now())
                           .where(OverdueBill.collection_id == application.id,
                                  OverdueBill.sub_bill_id == sub_bill_id)
                           .execute())
        # ?????????????????????,????????????????????????
        overdue_bill = (OverdueBill.select()
                        .where(OverdueBill.collection_id == application.id,
                               OverdueBill.status != 2,
                               OverdueBill.sub_bill_id != sub_bill_id))
        if overdue_bill.exists():
            if application.latest_bomber_id:
                Inbox.create(
                    title='application %s sub_bill_id %s cleared' % (
                        application.external_id, sub_bill_id),
                    content='application %s sub_bill_id %s cleared' % (
                        application.external_id, sub_bill_id),
                    receiver=application.latest_bomber_id,
                    category=InboxCategory.CLEARED.value,
                )
            return

        # ?????????????????????????????????
        partner = DispatchApp.filter(DispatchApp.application == application.id)
        if partner.exists():
            DispatchApp.update(
                status=DisAppStatus.ABNORMAL.value
            ).where(DispatchApp.application == application.id).execute()

        # ?????????????????????????????? application ??????
        AutoCallList.update(
            status=AutoListStatus.REMOVED.value,
            description='bill clear'
        ).where(AutoCallList.application == application.id).execute()

        application.status = ApplicationStatus.REPAID.value
        application.finished_at = datetime.now()
        application.paid_at = datetime.now()
        # ?????????????????????0???????????????????????????????????????bomber
        if int(bill.get("overdue_days")) <= 0:
            application.no_active = 1
            (RepaymentLog.update(no_active=1)
             .where(RepaymentLog.application == application.id)
             .execute())
        application.save()

        bomber_id = application.latest_bomber_id
        # c1b???????????????????????????,???????????????????????????2????????????bomber_id
        if (application.cycle in (Cycle.C1A.value,Cycle.C1B.value) and
                not bomber_id):
            bomber_id = application.cycle
        if not bomber_id:
            return

        (DispatchAppHistory.update(
            out_at=datetime.now()
        ).where(
            DispatchAppHistory.application == application.id,
            DispatchAppHistory.bomber_id == bomber_id)).execute()

        if not application.latest_bomber_id:
            return

        item = (OldLoanApplication
                .get_or_none(OldLoanApplication.status ==
                             OldLoanStatus.PROCESSING.value,
                             OldLoanApplication.application_id ==
                             application.id))
        if item:
            end_old_application(item, paid=True)
            out_record(src_bomber_id=bomber_id,
                       application_ids=[item.application_id])

        Inbox.create(
            title='application %s cleared' % application.external_id,
            content='application %s cleared' % application.external_id,
            receiver=application.latest_bomber_id,
            category=InboxCategory.CLEARED.value,
        )


# ??????bill2
@action(MessageAction.OVERDUE_BILL_SYNC)
def overdue_bill_sync(payload, msg_id):
    """?????????"""
    bill2_list = payload
    updated_count = 0
    with db.atomic():
        for bill in bill2_list:

            principal = Decimal(bill['principal'])
            repay_at = str_no_utc_datetime(bill['latest_repay_at'])

            updated_count += Application.update(
                amount=principal,
                repay_at=repay_at,
            ).where(Application.id == bill['external_id']).execute()

        logging.info('overdue sync done, updated count: %s', updated_count)


@action(MessageAction.BOMBER_CALC_OVERDUE_DAYS_OVER)
def calc_overdue_days_over(payload, msg_id):
    """
    Call by BOMBER_CALC_SUMMARY
    :param payload:
    :param msg_id:
    :return:
    """
    #????????????????????????95?????????
    now = fn.NOW()
    origin_diff_days = fn.DATEDIFF(now, Application.origin_due_at)
    overdue_days = fn.GREATEST(origin_diff_days, SQL('0'))
    query = (Application
             .update(overdue_days=overdue_days)
             .where(Application.status <<
                    [ApplicationStatus.PROCESSING.value,
                     ApplicationStatus.UNCLAIMED.value,
                     ApplicationStatus.AB_TEST.value],
                    Application.overdue_days > 95,
                    Application.type == ApplicationType.CASH_LOAN.value))
    updated_rows_count = query.execute()
    logging.info('calc overdue days done, updated count: %s',
                 updated_rows_count)

    try:
        calc_overdue_days_over_instalment()
    except Exception as e:
        logging.error("calc_overdue_days_over_instalment_error: %s"%str(e))

    # ??????overdue_days?????????????????????
    apps = Application.filter(
        Application.status << [ApplicationStatus.UNCLAIMED.value,
                               ApplicationStatus.PROCESSING.value,
                               ApplicationStatus.AB_TEST.value],
        Application.overdue_days > 95,
        Application.promised_date.is_null(True) |
        (fn.DATE(Application.promised_date) < datetime.today().date()))
    ids = [i.id for i in apps]
    for idx in range(0, len(ids), 100):
        send_to_default_q(
            MessageAction.BOMBER_AUTOMATIC_ESCALATION,
            {'application_list': ids[idx:idx + 100]})
    send_to_default_q(MessageAction.UPDATE_OLD_LOAN_APPLICATION, {})

# ????????????????????????95????????????????????????
def calc_overdue_days_over_instalment():
    now = fn.NOW()
    origin_diff_days = fn.DATEDIFF(now, OverdueBill.origin_due_at)
    overdue_days = fn.GREATEST(origin_diff_days, SQL('0'))
    sub_bill_status_list = [ApplicationStatus.PROCESSING.value,
                            ApplicationStatus.UNCLAIMED.value,
                            ApplicationStatus.AB_TEST.value]
    for status in sub_bill_status_list:
        # ??????????????????
        query = (OverdueBill.update(overdue_days=overdue_days)
                 .where(OverdueBill.status == status,
                        OverdueBill.overdue_days > 95))
        updated_rows_count = query.execute()
        logging.info("calc_overdue_days_over_instalment done,count:%s,status:%s" %
                     (updated_rows_count, status))

        # ??????????????????????????????
        overdue_bills = (OverdueBill
                         .select(OverdueBill.collection_id,
                                 OverdueBill.overdue_days)
                         .join(Application, JOIN_LEFT_OUTER,
                               on=OverdueBill.collection_id == Application.id)
                         .where(Application.status == status,
                                (Application.type ==
                                 ApplicationType.CASH_LOAN_STAGING.value)))
        # ???????????????????????????????????????????????????
        app_update = {}
        for ob in overdue_bills:
            if ob.collection_id not in app_update:
                app_update[ob.collection_id] = ob.overdue_days
            else:
                ob_days = max(app_update[ob.collection_id], ob.overdue_days)
                app_update[ob.collection_id] = ob_days
        # ??????????????????????????????
        for aid, a_days in app_update.items():
            q = (Application.update(overdue_days=a_days)
                 .where(Application.id == aid)
                 .execute())
    logging.info("update instalment application done")



@action(MessageAction.BOMBER_CALC_OVERDUE_DAYS)
def calc_overdue_days(payload, msg_id):
    """
    Call by BOMBER_CALC_SUMMARY
    :param payload:
    :param msg_id:
    :return:
    """
    now = fn.NOW()
    origin_diff_days = fn.DATEDIFF(now, Application.origin_due_at)
    overdue_days = fn.GREATEST(origin_diff_days, SQL('0'))
    query_unclaimed = (Application
                       .update(overdue_days=overdue_days)
                       .where(Application.status ==
                              ApplicationStatus.UNCLAIMED.value,
                              Application.overdue_days <= 95,
                              (Application.type ==
                               ApplicationType.CASH_LOAN.value)))
    updated_rows_count_unclaimed = query_unclaimed.execute()
    logging.info('calc overdue days done, updated count: %s',
                 updated_rows_count_unclaimed)

    query_processing = (Application
                        .update(overdue_days=overdue_days)
                        .where(Application.status ==
                               ApplicationStatus.PROCESSING.value,
                               Application.overdue_days <= 95,
                               (Application.type ==
                                ApplicationType.CASH_LOAN.value)))
    updated_rows_count_processing = query_processing.execute()
    logging.info('calc overdue days done, updated count: %s',
                 updated_rows_count_processing)

    query_test = (Application
                  .update(overdue_days=overdue_days)
                  .where(Application.status ==
                         ApplicationStatus.AB_TEST.value,
                         Application.overdue_days <= 95,
                         (Application.type ==
                          ApplicationType.CASH_LOAN.value)))
    updated_rows_count_test = query_test.execute()
    logging.info('calc overdue days done, updated count: %s',
                 updated_rows_count_test)

    # ??????????????????????????????
    calc_overdue_days_instalment()

    # ??????overdue_days?????????????????????
    apps = Application.select(Application.id).where(
                Application.status << [ApplicationStatus.UNCLAIMED.value,
                                       ApplicationStatus.PROCESSING.value,
                                       ApplicationStatus.AB_TEST.value],
                Application.overdue_days <= 95,
                Application.promised_date.is_null(True) |
                (fn.DATE(Application.promised_date) < datetime.today().date()))
    ids = [i.id for i in apps]
    for idx in range(0, len(ids), 100):
        send_to_default_q(
            MessageAction.BOMBER_AUTOMATIC_ESCALATION,
            {'application_list': ids[idx:idx + 100]})
    send_to_default_q(MessageAction.UPDATE_OLD_LOAN_APPLICATION, {})

    # overdue_days ????????????????????????C1A_entry(???????????????4?????????C1A)
    Application.update(
        C1A_entry=datetime.now()
    ).where(
        Application.status << [ApplicationStatus.UNCLAIMED.value,
                               ApplicationStatus.PROCESSING.value,
                               ApplicationStatus.AB_TEST.value],
        Application.overdue_days == 4
    ).execute()

# ??????????????????????????????
def calc_overdue_days_instalment():
    now = fn.NOW()
    origin_diff_days = fn.DATEDIFF(now, OverdueBill.origin_due_at)
    overdue_days = fn.GREATEST(origin_diff_days, SQL('0'))
    sub_bill_status_list = [ApplicationStatus.PROCESSING.value,
                            ApplicationStatus.UNCLAIMED.value,
                            ApplicationStatus.AB_TEST.value]
    # ??????????????????????????????
    today_now_time = datetime.now()
    month_first_day = today_now_time.replace(day=1,
                                             hour=1,
                                             minute=30,
                                             second=0,
                                             microsecond=0)
    for status in sub_bill_status_list:
        # ??????????????????
        query = (OverdueBill.update(overdue_days = overdue_days)
                 .where(OverdueBill.status == status,
                        OverdueBill.overdue_days <= 95))
        updated_rows_count = query.execute()
        logging.info("calc_overdue_days_instalment done,count:%s,status:%s" %
                     (updated_rows_count, status))

        # ??????????????????????????????
        overdue_bills = (OverdueBill
                         .select(OverdueBill.status,
                                 OverdueBill.created_at,
                                 OverdueBill.collection_id,
                                 OverdueBill.overdue_days)
                         .join(Application, JOIN_LEFT_OUTER,
                               on=OverdueBill.collection_id == Application.id)
                         .where(Application.status == status,
                                (Application.type ==
                                 ApplicationType.CASH_LOAN_STAGING.value)))
        # ???????????????????????????????????????????????????
        app_update = {}
        for ob in overdue_bills:
            # ??????????????????????????????????????????????????????
            if (ob.status == ApplicationStatus.REPAID.value and
                    ob.created_at < month_first_day):
                continue
            if ob.collection_id not in app_update:
                app_update[ob.collection_id] = ob.overdue_days
            else:
                ob_days = max(app_update[ob.collection_id],ob.overdue_days)
                app_update[ob.collection_id] = ob_days
        # ??????????????????????????????
        for aid,a_days in app_update.items():
            q = (Application.update(overdue_days = a_days)
                 .where(Application.id == aid)
                 .execute())
    logging.info("update instalment application done")


@action(MessageAction.BOMBER_AUTOMATIC_ESCALATION)
def automatic_escalation(payload, msg_id):
    app_ids = payload.get('application_list', [])
    if not app_ids:
        return
    # ???????????????????????????
    apps = (Application.select()
            .where(Application.id.in_(app_ids),
                   Application.status != ApplicationStatus.REPAID.value))

    for a in apps:
        new_cycle = application_entry_different_calculations(a)
        if a.overdue_days < 90:
            logging.info(
                "automatic_escalation_bomber_app_id:{},new_cycle:{},cycle:{},overdue_days:{}".format(
                    a.id, new_cycle, a.cycle, a.overdue_days))
        if new_cycle > a.cycle:
            with db.atomic():
                if (a.latest_bomber_id or
                        a.cycle in (Cycle.C1A.value, Cycle.C1B.value)):
                    bomber_id = (a.latest_bomber_id
                                 if a.latest_bomber_id else a.cycle)
                    (DispatchAppHistory.update(
                        out_at=datetime.now(),
                        out_overdue_days=a.overdue_days,
                    ).where(
                        DispatchAppHistory.application == a.id,
                        DispatchAppHistory.bomber_id == bomber_id
                    )).execute()

                Escalation.create(
                    application=a.id,
                    type=EscalationType.AUTOMATIC.value,
                    status=ApprovalStatus.APPROVED.value,
                    current_cycle=a.cycle,
                    escalate_to=new_cycle,
                    current_bomber_id=a.latest_bomber,
                )

                # ??????????????????????????????????????????dispatch_app????????????
                dis_app_update = (DispatchApp
                                  .update(status = DisAppStatus.ABNORMAL.value)
                                  .where(DispatchApp.application == a.id))
                dis_app_update.execute()
                a.cycle = new_cycle
                a.last_bomber = a.latest_bomber
                a.status = ApplicationStatus.UNCLAIMED.value
                a.latest_bomber = None
                a.ptp_bomber = None
                a.latest_call = None
                # ???????????? ??????????????????
                a.called_times = 0
                if new_cycle == Cycle.C1B.value:
                    a.C1B_entry = datetime.now()
                elif new_cycle == Cycle.C2.value:
                    a.C2_entry = datetime.now()
                elif new_cycle == Cycle.C3.value:
                    a.C3_entry = datetime.now()
                a.save()
    logging.info('automatic escalation done')

# ?????????????????????C1B???????????????10???
def application_entry_different_calculations(app):
    conf = {
        1: [1, 10],
        2: [11, 30],
        3: [31, 60],
        4: [61, 90],
        5: [91, 999999],
    }
    for new_cycle,scopes in conf.items():
        if scopes[0] <= app.overdue_days <= scopes[1]:
            return new_cycle
    return app.cycle



@action(MessageAction.BOMBER_CALC_SUMMARY)
def cron_summary(payload, msg_id):
    """?????????"""
    employees = Bomber.select(Bomber, Role).join(Role)
    summary = {
        i.id: {
            'cycle': i.role.cycle,
            'claimed': 0,
            'completed': 0,
            'cleared': 0,
            'escalated': 0,
            'transferred': 0,
            'promised': 0,
            'amount_recovered': Decimal(0),
            'calls_made': 0,
            'calls_connected': 0,
            'sms_sent': 0,
        }
        for i in employees
    }
    # ?????? 2??? 15??? ?????? ???????????????
    now_date = date.today()
    cal_date = now_date - timedelta(days=1)
    # ??????????????????ptp
    claimed = (Application
               .select(Application.latest_bomber,
                       fn.COUNT(Application.id).alias('claimed'))
               .where(fn.DATE(Application.claimed_at) == cal_date,
                      Application.status <<
                      [ApplicationStatus.PROCESSING.value,
                       ApplicationStatus.REPAID.value],
                      Application.latest_bomber.is_null(False))
               .group_by(Application.latest_bomber))

    # ??????ptp???????????????
    cleared = (Application
               .select(Application.latest_bomber,
                       fn.COUNT(Application.id).alias('cleared'))
               .where(fn.DATE(Application.finished_at) == cal_date,
                      Application.status == ApplicationStatus.REPAID.value,
                      Application.latest_bomber.is_null(False))
               .group_by(Application.latest_bomber))

    # ??????????????????ptp?????????
    completed = (Application
                 .select(Application.latest_bomber,
                         fn.COUNT(Application.id).alias('completed'))
                 .where(Application.latest_bombing_time.is_null(False),
                        fn.DATE(Application.latest_bombing_time) == cal_date,
                        Application.latest_bomber.is_null(False))
                 .group_by(Application.latest_bomber))

    # ?????????????????????????????????????????????cycle
    escalated = (Escalation
                 .select(Escalation.current_bomber,
                         fn.COUNT(Escalation.id).alias('escalated'))
                 .where(fn.DATE(Escalation.created_at) == cal_date,
                        Escalation.type == EscalationType.AUTOMATIC.value,
                        Escalation.current_bomber.is_null(False),
                        Escalation.status == ApprovalStatus.APPROVED.value)
                 .group_by(Escalation.current_bomber))

    # ???????????????????????????????????????
    transferred = (Transfer
                   .select(Transfer.operator,
                           fn.COUNT(Transfer.id).alias('transferred'))
                   .where(fn.DATE(Transfer.reviewed_at) == cal_date,
                          Transfer.status == ApprovalStatus.APPROVED.value)
                   .group_by(Transfer.operator))

    # ????????????p?????????????????????
    promised = (
        BombingHistory
        .select(BombingHistory.bomber,
                fn.COUNT(BombingHistory.id).alias('promised'))
        .where(fn.DATE(BombingHistory.created_at) == cal_date,
               BombingHistory.result == BombingResult.HAS_PROGRESS.value)
        .group_by(BombingHistory.bomber)
    )

    # ?????????????????????
    amount_recovered = (RepaymentLog
                        .select(RepaymentLog.current_bomber,
                                fn.SUM(RepaymentLog.principal_part)
                                .alias('principal_part'),
                                fn.SUM(RepaymentLog.late_fee_part)
                                .alias('late_fee_part'))
                        .where(fn.DATE(RepaymentLog.repay_at) == cal_date,
                               RepaymentLog.is_bombed == True,
                               RepaymentLog.current_bomber.is_null(False))
                        .group_by(RepaymentLog.current_bomber))

    # calllog????????????
    calls_made = (CallLog
                  .select(CallLog.user_id,
                          fn.COUNT(CallLog.record_id).alias('calls_made'))
                  .where(fn.DATE(CallLog.time_start) == cal_date,
                         CallLog.system_type == '1')
                  .group_by(CallLog.user_id))

    # calllog????????????
    calls_connected = (CallLog
                       .select(CallLog.user_id,
                               fn.COUNT(CallLog.record_id)
                               .alias('calls_connected'))
                       .where(fn.DATE(CallLog.time_start) == cal_date,
                              CallLog.duration > 10,
                              CallLog.system_type == '1').
                       group_by(CallLog.user_id))

    # ???????????????????????????
    sms_sent = (ConnectHistory
                .select(ConnectHistory.operator,
                        fn.COUNT(ConnectHistory.id).alias('sms_sent'))
                .where(ConnectHistory.type.in_(ConnectType.sms()),
                       ConnectHistory.created_at >= cal_date,
                       ConnectHistory.created_at < now_date
                       )
                .group_by(ConnectHistory.operator))

    for i in claimed:
        summary[i.latest_bomber_id]['claimed'] += i.claimed

    for i in completed:
        summary[i.latest_bomber_id]['completed'] += i.completed

    for i in cleared:
        summary[i.latest_bomber_id]['cleared'] += i.cleared

    for i in escalated:
        summary[i.current_bomber_id]['escalated'] += i.escalated

    for i in transferred:
        summary[i.operator_id]['transferred'] += i.transferred

    for i in promised:
        summary[i.bomber_id]['promised'] += i.promised

    for i in amount_recovered:
        amount_recovered = i.principal_part + i.late_fee_part
        summary[i.current_bomber_id]['amount_recovered'] += amount_recovered

    for i in calls_made:
        summary[int(i.user_id)]['calls_made'] += i.calls_made

    for i in calls_connected:
        summary[int(i.user_id)]['calls_connected'] += i.calls_connected

    for i in sms_sent:
        summary[i.operator_id]['sms_sent'] += i.sms_sent

    insert_args = []
    for bomber_id, data in summary.items():
        insert_args.append({
            'bomber': bomber_id,
            'cycle': data['cycle'],
            'claimed': data['claimed'],
            'completed': data['completed'],
            'cleared': data['cleared'],
            'escalated': data['escalated'],
            'transferred': data['transferred'],
            'promised': data['promised'],
            'amount_recovered': data['amount_recovered'],
            'calls_made': data['calls_made'],
            'calls_connected': data['calls_connected'],
            'sms_sent': data['sms_sent'],
            'date': cal_date,
        })

    if insert_args:
        Summary.insert_many(insert_args).execute()

    cycle_args = []
    # cal new in
    # ?????? cycle ??????
    escalated_in = (Escalation
                    .select(Escalation.escalate_to,
                            fn.COUNT(Escalation.id).alias('escalated_in'))
                    .where(Escalation.status == ApprovalStatus.APPROVED.value,
                           fn.DATE(Escalation.created_at) == cal_date)
                    .group_by(Escalation.escalate_to))

    for i in escalated_in:
        cycle_args.append({
            'cycle': i.escalate_to,
            'escalated_in': i.escalated_in,
            'date': cal_date,
        })

    amount_recovered_total = (
        RepaymentLog
        .select(RepaymentLog.cycle,
                fn.SUM(RepaymentLog.principal_part).alias('principal_part'),
                fn.SUM(RepaymentLog.late_fee_part).alias('late_fee_part'))
        .where(fn.DATE(RepaymentLog.repay_at) == cal_date)
        .group_by(RepaymentLog.cycle)
    )

    for i in amount_recovered_total:
        amount_recovered_total = i.principal_part + i.late_fee_part
        cycle_args.append({
            'cycle': i.cycle,
            'amount_recovered_total': amount_recovered_total,
            'date': cal_date,
        })
    if cycle_args:
        Summary.insert_many(cycle_args).execute()

    logging.info('cal summary done')

    # ????????????????????? ????????????????????? ??????????????????
    send_to_default_q(MessageAction.BOMBER_CALC_OVERDUE_DAYS, {})


@action(MessageAction.BOMBER_CALC_SUMMARY2)
def cron_summary2(payload, msg_id):
    """????????????????????????????????????,?????????????????????"""
    cal_date = date.today() - timedelta(days=1)
    employees = Bomber.select(Bomber, Role).join(Role)
    auto_call_actions = (
        AutoCallActions
        .select(
            AutoCallActions.bomber,
            AutoCallActions.result,
            fn.COUNT(AutoCallActions.id).alias('count')
        )
        .where(fn.DATE(AutoCallActions.created_at) == cal_date)
    )

    amount_recovered = (RepaymentLog
                        .select(RepaymentLog.current_bomber,
                                fn.SUM(RepaymentLog.principal_part)
                                .alias('principal_part'),
                                fn.SUM(RepaymentLog.late_fee_part)
                                .alias('late_fee_part'))
                        .where(fn.DATE(RepaymentLog.repay_at) == cal_date,
                               RepaymentLog.current_bomber.is_null(False),
                               RepaymentLog.is_bombed == True))

    cleared = (Application
               .select(Application.latest_bomber,
                       fn.COUNT(Application.id).alias('cleared'))
               .where(fn.DATE(Application.finished_at) == cal_date,
                      Application.status == ApplicationStatus.REPAID.value,
                      Application.latest_bomber.is_null(False)))

    auto_call_actions = auto_call_actions.group_by(
        AutoCallActions.bomber, AutoCallActions.result
    )
    amount_recovered = amount_recovered.group_by(RepaymentLog.current_bomber)
    cleared = cleared.group_by(Application.latest_bomber)

    summary = {
        e.id: {
            'cycle': e.role.cycle,
            'answered_calls': 0,
            'ptp': 0,
            'follow_up': 0,
            'not_useful': 0,
            'cleared': 0,
            'amount_recovered': 0,
        }
        for e in employees
    }
    for a in auto_call_actions:
        summary[a.bomber_id]['answered_calls'] += a.count
        if a.result == AutoCallResult.PTP.value:
            summary[a.bomber_id]['ptp'] += a.count
        if a.result == AutoCallResult.FOLLOW_UP.value:
            summary[a.bomber_id]['follow_up'] += a.count
        if a.result == AutoCallResult.NOT_USEFUL.value:
            summary[a.bomber_id]['not_useful'] += a.count

    for i in amount_recovered:
        amount_recovered = i.principal_part + i.late_fee_part
        summary[i.current_bomber_id]['amount_recovered'] += amount_recovered

    for i in cleared:
        summary[i.latest_bomber_id]['cleared'] += i.cleared

    insert_args = []
    for bomber_id, data in summary.items():
        insert_args.append({
            'bomber': bomber_id,
            'cycle': data['cycle'],
            'answered_calls': data['answered_calls'],
            'ptp': data['ptp'],
            'follow_up': data['follow_up'],
            'not_useful': data['not_useful'],
            'cleared': data['cleared'],
            'amount_recovered': str(data['amount_recovered']),
            'date': cal_date,
        })

    if insert_args:
        Summary2.insert_many(insert_args).execute()


@action(MessageAction.BOMBER_SYNC_CONTACTS)
def sync_suggested_contacts(payload, msg_id):
    """ suggested contacts sync """

    applications = (Application
                    .select(Application.id, Application.user_id)
                    .where(Application.status <<
                           [ApplicationStatus.UNCLAIMED.value,
                            ApplicationStatus.PROCESSING.value]))

    logging.debug('start sync contact')
    for a in applications:
        sync_contacts(a)
    logging.info('contact sync finished')


def sync_contacts(application):
    logging.info('application %s start sync contact', application.id)

    # ?????????????????????
    contacts = Contact.filter(Contact.user_id == application.user_id)
    existing_numbers = {contact.number for contact in contacts}

    # sms contacts
    insert_contacts = []
    sms_contacts = GoldenEye().get(
        '/applications/%s/sms-contacts' % application.external_id
    )
    if not sms_contacts.ok:
        sms_contacts = []
        logging.info('get user %s sms contacts failed', application.external_id)
    else:
        sms_contacts = sms_contacts.json()['data']

    for i in sms_contacts:
        if i['number'] in existing_numbers:
            continue
        insert_contacts.append({
            'user_id': application.user_id,
            'name': i['name'],
            'number': i['number'],
            'relationship': Relationship.SUGGESTED.value,
            'source': 'sms contacts',
            'real_relationship': Relationship.SUGGESTED.value
        })
        existing_numbers.add(i['number'])

    if insert_contacts:
        Contact.insert_many(insert_contacts).execute()

    # call frequency
    insert_contacts = []
    cf = GoldenEye().get(
        '/applications/%s/call/frequency' % application.external_id
    )
    if not cf.ok:
        call_frequency = []
        logging.error('get application %s call frequency error',
                      application.external_id)
    else:
        call_frequency = cf.json()['data']

    with db.atomic():
        for i in call_frequency:
            if i['number'] in existing_numbers:
                (Contact
                 .update(total_count=i['total_count'],
                         total_duration=i['total_duration'])
                 .where(Contact.number == i['number'],
                        Contact.user_id == application.user_id))
                continue

            insert_contacts.append({
                'user_id': application.user_id,
                'name': i['name'],
                'number': i['number'],
                'relationship': Relationship.SUGGESTED.value,
                'total_count': i['total_count'],
                'total_duration': i['total_duration'],
                'source': 'call frequency',
                'real_relationship': Relationship.SUGGESTED.value
            })
        if insert_contacts:
            Contact.insert_many(insert_contacts).execute()


@action(MessageAction.BOMBER_AUTO_SMS)
@deprecated(version='1.0', reason='This function will be removed soon')
def bomber_auto_sms(payload, msg_id):
    day_diff = int(payload['day_diff'])
    custom_type = payload.get('custom_type')
    msg_type = payload['msg_type']
    logging.info('auto sms %s sending', msg_type)

    applications = (
        Application
        .select()
        .where(Application.overdue_days == day_diff,
               Application.status << [ApplicationStatus.PROCESSING.value,
                                      ApplicationStatus.UNCLAIMED.value],
               Application.promised_date.is_null(True) |
               (fn.DATE(Application.promised_date) < datetime.today().date()))
    )

    if custom_type == 'new':
        applications = applications.where(Application.loan_success_times < 3)
    if custom_type == 'old':
        applications = applications.where(Application.loan_success_times >= 3)

    templates = (
        Template.select(Template.text, Template.app)
        .where(Template.type == ConnectType.AUTO_SMS.value,
               Template.id << Template.get_auto_sms_tpl(msg_type))
    )
    tpl_text = dict()
    for tpl in templates:
        tpl_text[tpl.app] = tpl.text

    data_list = []
    for a in applications:
        tpl_data = {
            'user_name': a.user_name,
            'due_days': a.overdue_days,
            'app_name': a.app,
            'phone': a.user_mobile_no,
            'cs_number': cs_number_conf.get(a.app, '02150202889'),
        }
        content = tpl_text[a.app].format(**tpl_data)
        data_list.append({
            'phone': '62' + a.user_mobile_no,
            'content': content,
            'app': a.app,
        })

    if not data_list:
        logging.info('auto sms %s do not need sending', msg_type)
        return

    send_sms(data_list, msg_type, SmsChannel.NUSA.value)


@action(MessageAction.BOMBER_AUTO_MESSAGE_DAILY)
def bomber_auto_message_daily(payload, msg_id):
    app_dict = dict(zip(AppName.keys(), AppName.values()))

    #???????????????????????????????????????
    auto_call_list = AutoCallActionsR \
        .select(AutoCallActionsR.application_id) \
        .where(fn.DATE(AutoCallActionsR.created_at) == fn.CURDATE())
    applications = (
        ApplicationR
        .select()
        .where(ApplicationR.overdue_days < 30,
               ApplicationR.overdue_days > 4,
               ApplicationR.type == ApplicationType.CASH_LOAN.value,
               ApplicationR.status << [ApplicationStatus.PROCESSING.value,
                                       ApplicationStatus.UNCLAIMED.value,
                                       ApplicationStatus.AB_TEST.value],
               ApplicationR.promised_date.is_null(True) |
               (fn.DATE(ApplicationR.promised_date) < datetime.today().date()),
               ~(ApplicationR.id << auto_call_list))
    )
    stage_list1 = range(*AutoCallMessageCycle.NEW_STAGE1.value['scope'], 3) #5,8,11,14
    stage_list2 = range(*AutoCallMessageCycle.STAGE2.value['scope'], 3) #15,18
    stage_list3 = range(*AutoCallMessageCycle.STAGE3.value['scope'], 3)
    sms_list = defaultdict(list)
    fcm_list = defaultdict(list)
    for a in applications:
        overdue_type = ''
        if a.overdue_days in stage_list1:
            if a.loan_success_times < 3:
                overdue_type = AutoCallMessageCycle.NEW_STAGE1.value['type']
            else:
                overdue_type = AutoCallMessageCycle.OLD_STAGE1.value['type']
        if a.overdue_days in stage_list2:
            overdue_type = AutoCallMessageCycle.STAGE2.value['type']
        if a.overdue_days in stage_list3:
            overdue_type = AutoCallMessageCycle.STAGE3.value['type']
        if overdue_type == '':
            continue
        # format app name
        app_name = app_dict.get(a.app.upper(), AppName.default().value)
        try:
            tpl_id = Template.get_daily_auto_sms_tpl(overdue_type, app_name)
        except KeyError:
            logging.warning('Key error {}, id is {}'.format(
                (overdue_type, app_name), a.id))
            continue
        data_map = {
                'user_name': a.user_name,
                'app_name': app_name,
                'overdue_days': a.overdue_days,
                'cs_number': cs_number_conf.get(a.app, '')
            }
        sms_list[(overdue_type, tpl_id, a.app)].append({
            'receiver': '62' + a.user_mobile_no,
            'data_map': data_map
        })
        fcm_list[(overdue_type, tpl_id, a.app)].append({
            'receiver': a.user_id,
            'data_map': data_map
        })

    for (msg_type, tpl_id, app_name), data_list in sms_list.items():
        auto_send_sms_and_fcm(data_list, tpl_id, app_name, "SMS")
    for (msg_type, tpl_id, app_name), data_list in sms_list.items():
        auto_send_sms_and_fcm(data_list, tpl_id, app_name, "FCM")


#??????????????????
@action(MessageAction.BOMBER_INSTALMENT_AUTO_MESSAGE_DAILY)
def bomber_instalment_auto_message_daily(payload, msg_id):
    applications = (ApplicationR.select(ApplicationR.id,
                                        ApplicationR.app,
                                        ApplicationR.user_id,
                                        ApplicationR.user_name,
                                        ApplicationR.user_mobile_no,
                                        ApplicationR.loan_success_times,
                                        OverdueBillR.status,
                                        OverdueBillR.sub_bill_id,
                                        OverdueBillR.overdue_days, )
                    .join(OverdueBillR, JOIN_LEFT_OUTER,
                          on=ApplicationR.id == OverdueBillR.collection_id)
                    .where(ApplicationR.type ==
                           ApplicationType.CASH_LOAN_STAGING.value,
                           ApplicationR.status != ApplicationStatus.REPAID.value,
                           ApplicationR.overdue_days < 90,
                           ApplicationR.promised_date.is_null(True) |
                           (fn.DATE(
                               ApplicationR.promised_date) < datetime.today().date()),
                           )
                    .dicts())
    # ??????????????????????????????????????????
    app_overdues = {}
    for app in applications:
        if app["status"] == ApplicationStatus.REPAID.value:
            continue
        if app["id"] in app_overdues:
            overdue_days = app_overdues[app["id"]]["overdue_days"]
            app_overdues[app["id"]]["overdue_days"] = max(app["overdue_days"],
                                                          overdue_days)
            app_overdues[app["id"]]["bill_sub_ids"].append(app["sub_bill_id"])
        else:
            app_overdues[app["id"]] = {
                "app_name": app["app"],
                "user_id": app["user_id"],
                "user_name": app["user_name"],
                "overdue_days": app["overdue_days"],
                "bill_sub_ids": [app["sub_bill_id"]],
                "phone": '62' + app["user_mobile_no"],
                "loan_success_times": app["loan_success_times"],
                "cs_number": cs_number_conf.get(app["app"], '02150202889')
            }
    # ??????????????????????????????????????????????????????????????????
    sms_dict = {}
    sub_bill_ids = []
    send_message = defaultdict(list)
    send_fcm = defaultdict(list)
    for aid, app in app_overdues.items():
        message_id = Template.get_daily_instalment_auto_sms_tpl(
            overdue_days=app["overdue_days"],
            loan_times=app["loan_success_times"]
        )
        if message_id:
            app["tpl_id"] = message_id
            sms_dict[aid] = app
            sub_bill_ids.extend(app["bill_sub_ids"])
    if not sms_dict:
        logging.info("no application need send sms")
        return
    sub_bills = []
    try:
        for index in range(0,len(sub_bill_ids),30):
            sub_bill = BillService().sub_bill_list(
                bill_sub_ids=sub_bill_ids[index:index+30])
            sub_bills += sub_bill
    except Exception as e:
        logging.info("send sms get bill error:%s" % str(e))
        return
    sub_bills_dict = {int(sb["id"]): sb for sb in sub_bills}
    for aid, app in sms_dict.items():
        amount = 0
        for sbid in app["bill_sub_ids"]:
            amount += sub_bills_dict.get(sbid, {}).get("unpaid", 0)
        data_map = {
                    "user_name": app["user_name"],
                    "app_name": app["app_name"],
                    "overdue_days": app["overdue_days"],
                    "cs_number": app["cs_number"],
                    "amount": str(amount)
                }
        send_message[(app['tpl_id'], app["app_name"])].append({
                "receiver": app["phone"],
                "data_map": data_map
            })
        send_fcm[(app['tpl_id'], app["app_name"])].append({
                "receiver": app["user_id"],
                "data_map": data_map
            })
    for (tpl_id, app_name), data_list in send_message.items():
        auto_send_sms_and_fcm(data_list, tpl_id, app_name, "SMS")
    for (msg_type, tpl_id, app_name), data_list in send_fcm.items():
        auto_send_sms_and_fcm(data_list, tpl_id, app_name, "FCM")



def auto_send_sms_and_fcm(data_list, tpl_id, app_name, message_type):
    if not data_list:
        return
    # 200 ??? ????????????
    for idx in range(0, len(data_list), 200):
        request_json = {
            "app_name": app_name,
            "failed_retry": True,
            "is_masking": True,
            "list": data_list[idx: idx+200],
            "message_level": 1,
            "message_type": message_type,
            "sms_type": 4 if message_type == "SMS" else 0,
            "type_id": tpl_id
        }
        try:
            result = MessageService().send_batch_template(**request_json)
            if not result.get("result"):
                logging.error()
        except Exception as e:
            logging.error()
            return
    logging.info("")


def get_danamall_msg_service(app_name, message_service):
    if app_name == AppName.DANAMALL.value:
        # token = app.config['service.message.%s.token' % app_name.lower()]
        message_service = Message(version=app_name)
    return message_service


#??????????????????????????????????????????
@action(MessageAction.BOMBER_REMIND_PROMISE)
def bomber_remind_promise(payload, msg_id):
    day_diff = int(payload['day_diff'])
    msg_type = payload['msg_type']
    logging.info('auto sms %s sending', msg_type)

    applications = (
        Application
        .select()
        .where(
            fn.DATEDIFF(fn.NOW(), Application.promised_date) == day_diff,
            Application.status << [
                ApplicationStatus.UNCLAIMED.value,
                ApplicationStatus.PROCESSING.value,
            ]
        )
    )

    templates = (
        Template
        .select(Template.text, Template.app)
        .where(Template.type == ConnectType.AUTO_SMS.value,
               Template.id << Template.get_auto_sms_tpl(msg_type))
    )

    tpl_text = {tpl.app: tpl.text for tpl in templates}
    message_date_dict = defaultdict(list)
    for a in applications:
        tpl_data = {
            'user_name': a.user_name,
            'due_days': a.overdue_days,
            'app_name': a.app,
            'phone': a.user_mobile_no,
            'cs_number': cs_number_conf.get(a.app, '02150202889'),
            'promised_date': a.promised_date.strftime('%d-%m-%Y'),
        }
        content = tpl_text[a.app].format(**tpl_data)
        message_date_dict[a.app].append(
            {
                "content": content,
                "receiver": '62' + a.user_mobile_no,
                "title": ""
            }
        )

    for app_name, data_list in message_date_dict.items():
        send_sms(data_list, msg_type, app_name)



@action(MessageAction.BOMBER_DISCOUNT_APPROVED)
def bomber_discount_approved(payload, msg_id):
    app_id = payload['id']
    msg_type = payload['msg_type']
    discount_to = payload['discount_to']
    effective_to = payload['effective_to']

    application = Application.filter(Application.id == app_id).first()
    if not application:
        logging.error('discount approved msg send failed '
                      'application %s not found', app_id)
        return
    template = (
        Template
        .select(Template.text, Template.app)
        .where(Template.type == ConnectType.AUTO_SMS.value,
               Template.id << Template.get_auto_sms_tpl(msg_type),
               Template.app == application.app)
        .first()
    )
    if not template:
        logging.error('discount approved msg send failed '
                      'template %s not found', msg_type)
        return

    promised_date = None
    if application.promised_date:
        promised_date = application.promised_date.strftime('%d-%m-%Y')
    tpl_data = {
        'user_name': application.user_name,
        'due_days': application.overdue_days,
        'app_name': application.app,
        'phone': application.user_mobile_no,
        'cs_number': cs_number_conf.get(application.app, '02150202889'),
        'promised_date': promised_date,
        'discount_to': discount_to,
        'effective_to': effective_to,
    }
    content = template.text.format(**tpl_data)

    data_list = [{
        'receiver': '62' + application.user_mobile_no,
        'content': content,
        'title': "",
    }]
    send_sms(data_list, msg_type, application.app)


# ???????????????????????????
def send_sms(data_list, msg_type, app_name):
    if not data_list:
        return
    for index in range(0, len(data_list), 200):
        req_data = {
              "app_name": app_name,
              "failed_retry": True,
              "is_masking": True,
              "list": data_list[index: index+200],
              "message_level": 0,
              "message_type": "SMS",
              "sms_type": 3
            }
        try:
            result = MessageService().send_batch(**req_data)
            if not result.get("result"):
                logging.error(
                    "send_sms_failed:%s,req:%s,res:%s",msg_type,req_data,result)
        except Exception as e:
            logging.error(
                "send_sms_error:%s,req:%s,res:%s,error:%s" % (
                    msg_type, req_data, result, str(e)))
            return
    logging.info("send_sms_success:%s", msg_type)

#??????????????????????????????
@action(MessageAction.BOMBER_AUTO_CALL_LIST)
def bomber_auto_call_list(payload, msg_id):

    with db.atomic():
        #?????????????????????????????????????????????????????????????????????id,???bomber
        #??????????????????partner?????????????????????????????????bomber??????partner_id?????????????????????
        bomber_dispatch_app()

        # ???????????????,???????????????installment ??????????????????bomber
        dispatch_instalment_app()
    #????????????
    dis_apps = (DispatchApp
                .select(DispatchApp.application)
                .where(DispatchApp.status == DisAppStatus.NORMAL.value))

    c1_apps = (
        Application
        .select(Application.id,
                Application.cycle,
                Application.follow_up_date,
                Application.called_times)
        .where(
            Application.status.not_in([ApplicationStatus.REPAID.value,
                                      ApplicationStatus.AB_TEST.value]),
            Application.cycle == Cycle.C1A.value,
            Application.is_rejected == False,  # noqa
            Application.promised_date.is_null(True) |
            (fn.DATE(Application.promised_date) < datetime.today().date())
        ).order_by(Application.overdue_days, Application.apply_at)
    )
    dis_apps_ids = [da.application_id for da in dis_apps]

    insert_args = []

    for a in c1_apps:
        if a.id in dis_apps_ids:
            continue
        insert_args.append({
            'application': a.id,
            'cycle': a.cycle,
            'follow_up_date': a.follow_up_date,
            'called_times': 1 if a.called_times else 0,
            'description': 'init'
        })

    if not insert_args:
        logging.error('no application need auto call')

    #??????application?????????????????????auto_call_list
    with db.atomic():
        AutoCallList.delete().execute()
        for idx in range(0, len(insert_args), 100):
            AutoCallList.insert_many(insert_args[idx:idx + 100]).execute()

    for idx in range(0, len(insert_args), 100):
        application_list = [
            i['application']
            for i in insert_args[idx:idx + 100]
        ]
        #????????????????????????????????????
        send_to_default_q(
            MessageAction.BOMBER_AUTO_CALL_CONTACT,
            {'application_list': application_list}
        )

    logging.info('bomber generate auto call list finished')

    #?????????P??????????????????????????????????????????????????????????????????
    send_to_default_q(
        MessageAction.UPDATE_BOMBER_FOR_SPECIAL,
        {})


class ChangeBomberTool(object):
    @staticmethod
    def in_record(bomber_id, ids, bd):
        subquery = (Application
                    .select(Application.amount,
                            fn.NOW().alias('created_at'),
                            fn.NOW().alias('updated_at'),
                            Application.id.alias('application_id'),
                            R(str(bomber_id)).alias('bomber_id'),
                            fn.NOW().alias('entry_at'),
                            R('null').alias('partner_id'),
                            SQL('DATE_ADD(CURDATE(),INTERVAL 14 DAY)')
                            .alias('expected_out_time'),
                            Application.overdue_days.alias(
                                'entry_overdue_days'))
                    .where(Application.status !=
                           ApplicationStatus.REPAID.value,
                           Application.id << ids))

        (Application
         .update(latest_bomber=bomber_id)
         .where(Application.id.in_(ids))
         .execute())
        application_list = list(subquery)
        for idx in range(0, len(application_list), 1000):
            applications = application_list[idx:idx + 1000]
            insert_args = list(map(partial(lambda_result, dct=bd),
                                   applications))
            DispatchAppHistory.insert_many(insert_args).execute()

    @staticmethod
    def out_record(a, bd):
        _id = str(a.id)
        (DispatchAppHistory.update(
            out_at=datetime.now(),
            out_overdue_days=a.overdue_days,
            out_principal_pending=(
                    a.amount -
                    Decimal(bd[_id].get('principal_paid'))),
            out_late_fee_pending=(
                    bd[_id].get('late_fee') -
                    bd[_id].get('late_fee_paid')),
        )
            .where(
            DispatchAppHistory.application == a.id,
            DispatchAppHistory.bomber_id == a.latest_bomber_id
        )).execute()

        a.last_bomber = a.latest_bomber
        a.latest_bomber = None
        a.ptp_bomber = None
        a.latest_call = None
        a.called_times = 0
        a.save()

    @staticmethod
    def classify(l, b):
        if len(l) == 1:
            return l[0]
        _l = filter(lambda x: x['bomber'] != b, l)
        return min(_l, key=lambda x: len(x['ids']))


@action(MessageAction.UPDATE_BOMBER_FOR_SPECIAL)
def update_bomber_for_special(payload, msg_id):
    """
    cycle 1b ?????????DPD21??????????????????P??????????????????????????????????????????
    cycle 2 ?????????DPD46??????????????????P??????????????????????????????????????????
    cycle 3 ?????????dpd76??????????????????p??????????????????????????????????????????

    :param payload:
    :param msg_id:
    :return:
    """
    filter_list = {Cycle.C1B.value: {"overdue_days": 21, "role_id": 5},
                   Cycle.C2.value: {"overdue_days": 46, "role_id": 6},
                   Cycle.C3.value: {"overdue_days": 76, "role_id": 8}}
    cbt = ChangeBomberTool()
    for cycle, values in filter_list.items():
        overdue_days = values["overdue_days"]
        bombers = (Bomber.select()
                   .where(Bomber.role == values["role_id"],
                          Bomber.instalment == 0,
                          Bomber.is_del == 0))
        bids = {b.id:b for b in bombers}
        apps = (Application.select()
                .where(Application.cycle == cycle,
                       Application.type == ApplicationType.CASH_LOAN.value,
                       Application.overdue_days == overdue_days,
                       Application.status == ApplicationStatus.AB_TEST.value,
                       Application.promised_date.is_null(True) |
                       (fn.DATE(Application.promised_date) < date.today()),
                       Application.latest_bomber_id.in_(list(bids.keys()))))
        classify_dict = defaultdict(list)
        for b in bombers:
            classify_dict[b.group_id].append({"bomber": b.id, "ids": []})
        with db.atomic():
            app_ids = [i.id for i in apps]
            if app_ids and bids:
                bills = BillService().bill_list(application_ids=app_ids)
                bill_dict = {str(bill['application_id']): bill for bill in
                             bills}
                for i in apps:
                    current_bomber = bids.get(i.latest_bomber_id)
                    if not current_bomber:
                        continue
                    classify_list = classify_dict.get(current_bomber.group_id)
                    d = cbt.classify(classify_list, i.latest_bomber_id)
                    d["ids"].append(i.id)
                    cbt.out_record(i, bill_dict)
                for group_id, cl_list in classify_dict.items():
                    for item in cl_list:
                        cbt.in_record(item["bomber"], item["ids"], bill_dict)
            else:
                logging.info(
                    "cycle:{} empty application list {} or bomber list {}".format(
                        cycle, app_ids, list(bids.keys())))
    try:
        update_bomber_for_special_instalment()
    except Exception as e:
        logging.error("special_instalment_error:%s"%str(e))

# ??????c2,c3????????????
def update_bomber_for_special_instalment():
    filter_list = {Cycle.C1B.value: 21, Cycle.C2.value: 46, Cycle.C3.value: 76}
    for cycle,overdue_days in filter_list.items():
        # ??????????????????????????????
        bombers = (Bomber.select().where(Bomber.instalment == cycle,
                                         Bomber.is_del == 0))
        bids = {b.id:b for b in bombers}
        # ???????????????
        apps = (Application.select()
                .where(Application.cycle == cycle,
                       Application.status == ApplicationStatus.AB_TEST.value,
                       Application.type ==
                       ApplicationType.CASH_LOAN_STAGING.value,
                       Application.overdue_days == overdue_days,
                       Application.promised_date.is_null(True) |
                       (fn.DATE(Application.promised_date) < date.today()),
                       Application.latest_bomber_id.in_(list(bids.keys()))))

        classify_dict = defaultdict(list)
        for b in bombers:
            classify_dict[b.group_id].append({"bomber":b.id, "ids":[]})
        for a in apps:
            current_bomber = bids.get(a.latest_bomber_id)
            if not current_bomber:
                continue
            classify_list = classify_dict.get(current_bomber.group_id)
            d = ChangeBomberTool.classify(classify_list, a.latest_bomber_id)
            d["ids"].append(a.id)
        with db.atomic():
            for group_id,classify_list in classify_dict.items():
                for cl in classify_list:
                    aids = cl["ids"]
                    if not aids:
                        continue
                    latest_bomber_id = cl["bomber"]
                    q = (Application.update(latest_bomber = latest_bomber_id,
                                            last_bomber = Application.latest_bomber)
                         .where(Application.id << aids)
                         .execute())
                    record_param = {
                        "cycle": cycle,
                        "application_ids": aids,
                        "dest_bomber_id": latest_bomber_id,
                    }
                    out_and_in_record_instalment(**record_param)



def bomber_dispatch_app():

    # ????????????c1a????????????????????????????????????partner
    try:
        c1a_dispatch_app()
    except Exception as e:
        logging.error("c1a_dispatch_app error:%s"%str(e))

    cycle = {
        1: 10,
        2: 30,
        3: 60,
        4: 90
    }

    # ???????????? Cycle.C2 overdue_day 31
    apps = (Application.select()
            .where(fn.DATE(Application.C2_entry) == date.today(),
                   Application.type == ApplicationType.CASH_LOAN.value))

    partners = (Partner.select()
                .where(Partner.status == PartnerStatus.NORMAL.value,
                       Partner.cycle == Cycle.C2.value))

    apps_ids = [a.id for a in apps]
    dispatch_inserts = []
    start_index = 0
    apps_length = len(apps_ids)
    logging.warning('apps length %s' % str(apps_length))

    for p in partners:  # ???????????????partner
        bombers = (Bomber.select()
                   .where(Bomber.partner == p.id,
                          Bomber.status != BomberStatus.OUTER_LEADER.value,
                          Bomber.is_del == 0))

        gen = CycleIter([b.id for b in bombers])
        existing_list = []

        end_index = start_index + int(apps_length * p.app_percentage)
        logging.info('partner length %s' % str(end_index))

        if not apps_ids[start_index:end_index]:
            continue
        bills = BillService().bill_list(
            application_ids=apps_ids[start_index:end_index])
        bill_dict = {bill['application_id']: bill for bill in bills}

        for a_id in apps_ids[start_index:end_index]:
            bomber = average_gen(gen, existing_list)
            q = (DispatchApp.delete()
                 .where(DispatchApp.application == a_id)
                 .execute())
            dispatch_inserts.append({
                'application': a_id,
                'bomber': bomber,
                'partner': p.id,
            })

            # ????????????????????????????????????????????????????????????
            application = (Application.select()
                           .where(Application.id == a_id)).first()
            application.latest_bomber = bomber
            application.status = ApplicationStatus.AB_TEST.value
            application.ptp_bomber = None
            application.save()
            day_next_cycle = (cycle.get(application.cycle) -
                              application.overdue_days)
            DispatchAppHistory.create(
                application=a_id,
                partner_id=p.id,
                bomber_id=bomber,
                entry_at=datetime.now(),
                entry_overdue_days=application.overdue_days,
                entry_principal_pending=(
                    application.amount -
                    Decimal(bill_dict[a_id].get('principal_paid'))),
                entry_late_fee_pending=(
                    Decimal(bill_dict[a_id].get('late_fee')) -
                    Decimal(bill_dict[a_id].get('late_fee_paid'))),
                expected_out_time=(date.today() +
                                   timedelta(days=day_next_cycle))
            )

        start_index = end_index

    with db.atomic():
        for idx in range(0, len(dispatch_inserts), 100):
            DispatchApp.insert_many(dispatch_inserts[idx:idx + 100]).execute()

    # AB test ??????(??????????????????)

    config = SystemConfig.prefetch(SCI.AB_TEST_C2)
    c2_bomber = config.get(SCI.AB_TEST_C2, SCI.AB_TEST_C2.default_value)
    # ?????????????????????????????????????????????id [76, 100, 106, 107, 213, 215, 216, 221, 222, 223, 226, 235]
    c2_bomber = get_cash_bomber(c2_bomber, Cycle.C2.value)
    #python??????application  id
    c2 = apps_ids[start_index:]
    if c2:
        bills = BillService().bill_list(
            application_ids=c2)
    else:
        bills = []
    #java??????bill
    bill_dict = {bill['application_id']: bill for bill in bills}
    logging.info('c2 AB_test length: %s' % str(c2))
    gen = CycleIter(c2_bomber)
    existing_list = []
    for c in c2:
        bomber = average_gen(gen, existing_list)
        application = Application.filter(Application.id == c).first()
        application.status = ApplicationStatus.AB_TEST.value
        application.latest_bomber = bomber
        application.ptp_bomber = None
        application.save()

        day_next_cycle = 46 - application.overdue_days
        DispatchAppHistory.create(
            application=c,
            bomber_id=bomber,
            entry_at=datetime.now(),
            entry_overdue_days=application.overdue_days,
            entry_principal_pending=(application.amount
                                     - bill_dict[c].get('principal_paid', 0)),
            entry_late_fee_pending=(
                    bill_dict[c].get('late_fee', 0) -
                    bill_dict[c].get('late_fee_paid', 0)),
            expected_out_time=(date.today() + timedelta(days=day_next_cycle))
        )
    ab_test_other()



# ??????????????????????????????????????????C1a ??????????????????????????????
def c1a_dispatch_app():
    today = datetime.today().date()
    tomorrow = today + timedelta(days=1)
    #??????????????????
    c1a_apps = (Application.select()
                .where(Application.status << [ApplicationStatus.UNCLAIMED.value,
                                              ApplicationStatus.PROCESSING.value],
                       Application.dpd1_entry >= today,
                       Application.dpd1_entry < tomorrow,
                       Application.type == ApplicationType.CASH_LOAN.value))
    all_aids = [a.id for a in c1a_apps]
    # ??????????????????
    partners = (Partner.select()
                .where(Partner.status == PartnerStatus.NORMAL.value,
                       Partner.cycle == Cycle.C1A.value))
    end = 0
    for p in partners:
        #????????????partner ??????bomber
        bombers = (Bomber.select()
                   .where(Bomber.partner == p.id,
                          Bomber.is_del == 0))
        start = end
        end += int(len(all_aids) * p.app_percentage)
        aids = all_aids[start:end]
        bids = [b.id for b in bombers]
        if not bids or not aids:
            continue
        # ?????????????????????????????????????????????
        average_number = get_average_number(len(aids),len(bids))
        p_end = 0
        for i,bid in enumerate(bids):
            p_start = p_end
            p_end += average_number[i]
            b_aids = aids[p_start:p_end]
            with db.atomic():
                q = (Application
                     .update(latest_bomber = bid,
                             status = ApplicationStatus.AB_TEST.value)
                     .where(Application.id << b_aids)
                     .execute())
                params = {
                    "cycle": Cycle.C1A.value,
                    "dest_partner_id": p.id,
                    "application_ids": b_aids,
                    "dest_bomber_id": bid
                }
                new_in_record(**params)
            try:
                dispatch_inserts = []
                for aid in b_aids:
                    dispatch_inserts.append({'application': aid,
                                             'bomber': bid,
                                             'partner': p.id,
                                             'status': DisAppStatus.NORMAL.value})
                if dispatch_inserts:
                    q = (DispatchApp.insert_many(dispatch_inserts).execute())
            except Exception as e:
                logging.error("c1a????????????dispatch_app error:%s"%str(e))


def ab_test_other():
    cycle_upper = {
        1: 10,
        2: 30,
        3: 60,
        4: 76
    }

    c1b = (Application.select()
           .where(fn.DATE(Application.C1B_entry) == date.today(),
                  Application.type == ApplicationType.CASH_LOAN.value)
           .order_by(-Application.overdue_days)
           )
    c1b_id = [a.id for a in c1b]

    dis_app_update = (DispatchApp.update(status=DisAppStatus.ABNORMAL.value)
                      .where(DispatchApp.application.in_(c1b_id)))
    dis_app_update.execute()

    c3 = (Application.select()
          .where(fn.DATE(Application.C3_entry) == date.today(),
                 Application.type == ApplicationType.CASH_LOAN.value))
    all_id = [b.id for b in c3]

    try:
        # ???C3??????????????????????????????
        partners = (Partner.select()
                    .where(Partner.status == PartnerStatus.NORMAL.value,
                           Partner.cycle == Cycle.C3.value))

        start_index, end_index, out_apps = 0, 0, {}
        for p in partners:
            end_index += int(len(all_id) * p.app_percentage)
            out_apps[p.id] = all_id[start_index:end_index]
            start_index = end_index
        c3_id = all_id[end_index:]
        allot_c3_case(out_apps)
    except:
        c3_id = all_id

    config = SystemConfig.prefetch(SCI.AB_TEST_C1B, SCI.AB_TEST_C3)
    c1b_bomber = config.get(SCI.AB_TEST_C1B, SCI.AB_TEST_C1B.default_value)
    c3_bomber = config.get(SCI.AB_TEST_C3, SCI.AB_TEST_C3.default_value)
    # ??????????????????????????????
    c3_bomber = get_cash_bomber(c3_bomber, Cycle.C3.value)
    data = [{'ids': c1b_id, 'bomber': c1b_bomber, 'index': 0, 'cycle': 2},
            {'ids': c3_id, 'bomber': c3_bomber, 'index': 1, 'cycle': 4}]

    for d in data:
        applications = d.get('ids')
        length = len(applications)
        end = int(length * d.get('index'))
        gen = CycleIter(d.get('bomber'))
        existing_list = []
        if not applications:
            continue
        bills = BillService().bill_list(
            application_ids=applications)
        bill_dict = {bill['application_id']: bill for bill in bills}
        for a in applications[:end]:
            bomber = average_gen(gen, existing_list)
            application = Application.filter(Application.id == a).first()
            application.status = ApplicationStatus.AB_TEST.value
            application.latest_bomber = bomber
            application.ptp_bomber = None
            application.save()

            day_next_cycle = (cycle_upper.get(application.cycle) -
                              application.overdue_days)
            DispatchAppHistory.create(
                application=a,
                bomber_id=bomber,
                entry_at=datetime.now(),
                entry_overdue_days=application.overdue_days,

                entry_principal_pending=(application.amount -
                                         bill_dict[a]['principal_paid']),
                entry_late_fee_pending=(bill_dict[a]['late_fee'] -
                                        bill_dict[a]['late_fee_paid']),
                expected_out_time=(date.today() +
                                   timedelta(days=day_next_cycle))
            )

        # ??????partner???????????????????????????????????????
        if d.get('cycle') == Cycle.C1B.value:
            c1b_wb_partner = (Partner.select()
                                    .where(Partner.cycle == Cycle.C1B.value,
                                           Partner.status ==
                                           PartnerStatus.NORMAL.value))
            # ??????c1b????????????
            c1b_wb_p_dict = { str(p.id):p.app_percentage for p in c1b_wb_partner}
            c1b_wb_pids = list(map(int, c1b_wb_p_dict.keys()))
            c1b_wb_bombers = (Bomber.select()
                                    .where(Bomber.is_del == 0,
                                           Bomber.partner_id << c1b_wb_pids,
                                           Bomber.password.is_null(False)))
            # ?????????????????????????????????????????????????????????
            c1b_wb_pba = {}
            apps_num = len(applications)
            for cb in c1b_wb_bombers:
                cb_key = str(cb.partner_id)
                if cb_key in c1b_wb_pba:
                    c1b_wb_pba[cb_key]["bids"].append(cb.id)
                else:
                    # ???????????????????????????????????????????????????
                    start = end
                    percentage = c1b_wb_p_dict.get(cb_key, 0)
                    end = start + ceil(apps_num * percentage)
                    c1b_wb_pba[cb_key] = {
                        "bids": [cb.id],
                        "pid": cb.partner_id,
                        "apps": applications[start:end]
                    }
            # ???????????????c1b??????????????????
            inner_c1b_apps = applications[end:]
            dispatch_c1b_inner_apps(aids=inner_c1b_apps,
                                    bills=bill_dict,
                                    period=cycle_upper.get(Cycle.C1B.value))
            for pid,c1b_wb in c1b_wb_pba.items():
                c1b_wb_apps = c1b_wb["apps"]
                c1b_wb_bids = c1b_wb["bids"]
                average_nums = get_average_number(len(c1b_wb_apps),
                                                  len(c1b_wb_bids))
                bid_end = 0
                for b_index,bid in enumerate(c1b_wb_bids):
                    bid_start = bid_end
                    bid_end = bid_start + average_nums[b_index]
                    bid_apps = c1b_wb_apps[bid_start:bid_end]
                    logging.info("c1b_??????:bid:%s,bid_apps:%s"%(bid, bid_apps))
                    with db.atomic():
                        app_sql = (Application.update(latest_bomber=bid,
                                        status=ApplicationStatus.AB_TEST.value,
                                                      ptp_bomber=None)
                                    .where(Application.id << bid_apps))
                        app_sql.execute()
                        params = {
                            "apps":bid_apps,
                            "partner_id": int(pid),
                            "bill_dict": bill_dict,
                            "period": cycle_upper.get(Cycle.C1B.value),
                            "bomber_id":bid
                        }
                        c1b_dispatch_in_record(**params)
                    try:
                        for aid in bid_apps:
                            dispatch_inserts = {
                                'application': aid,
                                'bomber': bid,
                                'partner': int(pid),
                                'status': DisAppStatus.NORMAL.value,
                            }
                            q = (DispatchApp.update(**dispatch_inserts)
                                        .where(DispatchApp.application == aid)
                                        .execute())
                            if not q:
                                DispatchApp.create(**dispatch_inserts)
                    except Exception as e:
                        logging.error("dispatchApp????????????:%s"%str(e))


def allot_c3_case(out_data):
    dispatch_inserts = []
    for key, value in out_data.items():
        if not value:
            continue

        bombers = (Bomber
                   .filter(Bomber.partner == key,
                           Bomber.status == BomberStatus.OUTER.value,
                           Bomber.is_del == 0))
        bomber_ids = [b.id for b in bombers]
        bomber = CycleIter(bomber_ids)
        bills = BillService().bill_list(application_ids=value)
        bill_dict = {bill['application_id']: bill for bill in bills}

        for v in value:
            bomber_id = bomber.__next__()
            q = (DispatchApp.delete()
                 .where(DispatchApp.application == v)
                 .execute())
            dispatch_inserts.append({
                'application': v,
                'bomber': bomber_id,
                'partner': key,
            })

            # ???????????????????????????????????????
            application = (Application.filter(Application.id == v)).first()
            application.latest_bomber = bomber_id
            application.ptp_bomber = None
            application.status = ApplicationStatus.AB_TEST.value
            application.save()

            # c3???????????????cycle??????????????????90???
            day_next_cycle = (90 - application.overdue_days)
            DispatchAppHistory.create(
                application=v,
                partner_id=key,
                bomber_id=bomber_id,
                entry_at=datetime.now(),
                entry_overdue_days=application.overdue_days,
                entry_principal_pending=(
                        application.amount -
                        Decimal(bill_dict[v].get('principal_paid'))),
                entry_late_fee_pending=(
                        Decimal(bill_dict[v].get('late_fee')) -
                        Decimal(bill_dict[v].get('late_fee_paid'))),
                expected_out_time=(
                        date.today() + timedelta(days=day_next_cycle))
            )

    with db.atomic():
        for idx in range(0, len(dispatch_inserts), 100):
            DispatchApp.insert_many(dispatch_inserts[idx:idx + 100]).execute()

# ??????????????????????????????
def get_cash_bomber(bids, cycle):
    cash_bombers = (Bomber.select()
                    .where(Bomber.id << bids,
                           Bomber.is_del == 0,
                           Bomber.instalment != cycle))
    cash_bids = [b.id for b in cash_bombers]
    return cash_bids

# c1b ?????????????????????????????????
def dispatch_c1b_inner_apps(aids, bills, period=30):
    # ???????????????????????????
    bombers = (Bomber.select()
               .where(Bomber.role_id == 5,
                      Bomber.is_del == 0,
                      Bomber.instalment == 0))
    bids = [b.id for b in bombers]
    if not aids or not bids:
        return
    avg_num = get_average_number(len(aids),len(bids))
    end = 0
    with db.atomic():
        for index,b in enumerate(bids):
            start = end
            end = start + avg_num[index]
            b_aids = aids[start:end]
            app_sql = (Application.update(latest_bomber=b,
                                          status=ApplicationStatus.AB_TEST.value,
                                          ptp_bomber=None)
                       .where(Application.id << b_aids))
            app_sql.execute()
            params = {
                "apps": b_aids,
                "bill_dict": bills,
                "period": period,
                "bomber_id": b
            }
            c1b_dispatch_in_record(**params)

# ??????????????????????????????
def dispatch_instalment_app():

    cycle_list = [Cycle.C1B.value, Cycle.C2.value, Cycle.C3.value,Cycle.M3.value]
    # ????????????,????????????cycle?????????????????????
    for cycle in cycle_list:
        apps = (Application.select()
                .where(Application.cycle == cycle,
                       Application.latest_bomber.is_null(True),
                       Application.status != ApplicationStatus.REPAID.value,
                       (Application.type ==
                        ApplicationType.CASH_LOAN_STAGING.value)))
        aids = [a.id for a in apps]
        if not aids:
            continue
        # ???????????????bomber
        bombers = (Bomber.select()
                   .where(Bomber.is_del == 0,
                          Bomber.instalment == cycle))
        bids = [b.id for b in bombers]
        if not bids:
            continue
        average_nums = get_average_number(len(apps),len(bids))
        end = 0
        for i,bid in enumerate(bids):
            start = end
            end = start + average_nums[i]
            bid_apps = aids[start:end]
            with db.atomic():
                # ????????????
                q = (Application.update(ptp_bomber = None,
                                        latest_bomber = bid, #??????????????????id
                                        last_bomber = Application.latest_bomber,#????????????????????????
                                        status = ApplicationStatus.AB_TEST.value)#??????????????????
                     .where(Application.id << bid_apps)
                     .execute())
                record_param = {"cycle": cycle,
                                "application_ids": bid_apps,
                                "dest_bomber_id": bid}
                out_and_in_record_instalment(**record_param)


# ????????????????????????
def out_and_in_record_instalment(**kwargs):
    if not kwargs.get("application_ids"):
        return
    # ?????????
    out_q = (DispatchAppHistory.update(out_at = fn.NOW())
             .where(DispatchAppHistory.application << kwargs['application_ids'],
                    DispatchAppHistory.out_at.is_null(True))
             .execute())
    # ??????
    cycle_period = {
        1: '10',
        2: '30',
        3: '60',
        4: '90'
    }
    period = cycle_period.get(kwargs['cycle'], '90 + t1.overdue_days')
    kwargs['dest_partner_id'] = kwargs.get('dest_partner_id') or 'null'
    subquery = (Application
                .select(Application.amount,
                        fn.NOW().alias('created_at'),
                        fn.NOW().alias('updated_at'),
                        Application.id.alias('application_id'),
                        R(str(kwargs['dest_bomber_id'])).alias('bomber_id'),
                        fn.NOW().alias('entry_at'),
                        Application.overdue_days.alias('entry_overdue_days'),
                        R(str(kwargs['dest_partner_id'])).alias('partner_id'),
                        (SQL('DATE_ADD(CURDATE(),INTERVAL (%s -'
                             ' t1.overdue_days) DAY)' % period))
                        .alias('expected_out_time'))
                .where(Application.status != ApplicationStatus.REPAID.value,
                       Application.id << kwargs['application_ids']))
    application_list = list(subquery)
    for idx in range(0, len(application_list), 50):
        applications = application_list[idx:idx + 50]
        app_ids = [i.application_id for i in applications]
        # ???????????????overdue_bill
        overdue_bills = (OverdueBill.select()
                         .where(OverdueBill.collection_id << app_ids))
        sub_bill_ids = [ob.sub_bill_id for ob in overdue_bills]
        bill_list = BillService().sub_bill_list(bill_sub_ids=sub_bill_ids)
        insert_args = lambad_instalment_result(bill_list, applications)
        if not insert_args:
            continue
        DispatchAppHistory.insert_many(insert_args).execute()

#???????????????????????????
def lambad_instalment_result(bill_list,applications):
    bill_dict = {}
    insert_args = []
    # ??????????????????
    for sub_bill in bill_list:
        bill_id = sub_bill["bill_id"]
        principal_pending = sub_bill["amount"] - sub_bill['principal_paid']
        late_fee_pending = sub_bill["late_fee"] - sub_bill["late_fee_paid"]
        if bill_id in bill_dict:
            bill_dict[bill_id]["entry_principal_pending"] += principal_pending
            bill_dict[bill_id]["entry_late_fee_pending"] += late_fee_pending
        else:
            bill_dict[bill_id] = {
                "entry_principal_pending": principal_pending,
                "entry_late_fee_pending": late_fee_pending
            }

    for app in applications:
        bill_entry = bill_dict.get(app.bill_id, {})
        entry_principal_pending = bill_entry.get("entry_principal_pending", 0)
        entry_late_fee_pending = bill_entry.get("entry_late_fee_pending", 0)
        insert_dict = {
            'created_at': app.created_at,
            'updated_at': app.updated_at,
            'application': app.application_id,
            'bomber_id': app.bomber_id,
            'entry_at': app.entry_at,
            'entry_overdue_days': app.entry_overdue_days,
            'partner_id': app.partner_id,
            'expected_out_time': app.expected_out_time,
            'entry_principal_pending': entry_principal_pending,
            'entry_late_fee_pending': entry_late_fee_pending
        }
        insert_args.append(insert_dict)
    return insert_args


def c1b_dispatch_in_record(**kwargs):
    app_ids = kwargs.get("apps")
    partner_id = kwargs.get("partner_id","null")
    bill_dict = kwargs.get("bill_dict")
    period = kwargs.get("period")
    bomber_id = kwargs.get('bomber_id')
    if not all([app_ids, partner_id, bill_dict, period]):
        return False
    bill_dict = { str(k):v for k,v in bill_dict.items()}
    subquery = (Application
                .select(Application.amount,
                        fn.NOW().alias('created_at'),
                        fn.NOW().alias('updated_at'),
                        Application.id.alias('application_id'),
                        R(str(bomber_id)).alias('bomber_id'),
                        fn.NOW().alias('entry_at'),
                        Application.overdue_days.alias('entry_overdue_days'),
                        R(str(partner_id)).alias('partner_id'),
                        (SQL('DATE_ADD(CURDATE(),INTERVAL (%s -'
                             ' t1.overdue_days) DAY)' % period))
                        .alias('expected_out_time'))
                .where(Application.id << app_ids))
    application_list = list(subquery)
    for idx in range(0,len(application_list),1000):
        applications = application_list[idx:idx+1000]
        insert_args = list(map(partial(lambda_result,
                                       dct=bill_dict),
                               applications))
        DispatchAppHistory.insert_many(insert_args).execute()



#???????????????????????????
@action(MessageAction.BOMBER_AUTO_CALL_CONTACT)
def bomber_auto_call_contact(payload, msg_id):
    application_list = payload['application_list']
    applications = []
    for app_id in application_list:
        applications.append(Application.filter(Application.id == app_id)
                            .first())
    # ?????????????????????????????????
    with db.atomic():
        for application in applications:
            cycle = application.cycle
            # ????????????????????????
            contacts = (
                Contact
                .select()
                .where(Contact.user_id == application.user_id,
                       Contact.latest_status.not_in(ContactStatus.no_use()))
                .order_by(-Contact.useful,
                          Contact.relationship,
                          -Contact.total_duration,
                          -Contact.total_count)
            )

            level1 = []
            level2 = []
            level3 = []
            level = []
            for c in contacts:
                if c.relationship == Relationship.APPLICANT.value:
                    level.append(c)
                elif c.relationship == Relationship.FAMILY.value:
                    level1.append(c)
                elif c.relationship == Relationship.COMPANY.value:
                    level2.append(c)
                elif c.relationship == Relationship.SUGGESTED.value:
                    level3.append(c)

            contacts = level + level2 + level1 + level3

            numbers = []
            fc_count = 0

            # Pre-check if need phone calls????????????????????????????????????
            app_calls = []
            need_verify = False
            for eac_contact in contacts:
                if (eac_contact.relationship == Relationship.FAMILY.value and
                        eac_contact.useful == ContactsUseful.NONE.value):
                    need_verify = True
                    break

            if need_verify:
                logging.info('Found contact need update. app id {}'
                             .format(str(application.id)))
                app_calls = AuditService().phone_invalid(cat=Relationship(1).name,
                                        application_id=application.external_id)

            call_history = True
            c1b_family_dict = defaultdict(list)
            for c in contacts:
                if c.relationship == Relationship.COMPANY.value:
                    if cycle == Cycle.C1A.value:
                        call_history = check_call_history(application)
                        break
                    if cycle == Cycle.C1B.value:
                        # ??????c1b?????????????????????????????????
                        if c.source != CompanyContactType.BASIC_INFO_JOB_TEL.value:
                            continue
                if c.relationship == Relationship.FAMILY.value:
                    if cycle == Cycle.C1A.value:
                        call_history = check_call_history(application)
                        break

                    # Update contact useful
                    if c.useful == ContactsUseful.NONE.value:
                        c.useful = check_valid_phone(app_calls, c)
                        c.save()

                    if c.useful == ContactsUseful.INVALID.value:
                        logging.info('Found invalid contact. {}'
                                     .format(str(c.id)))
                        continue

                    # ?????????family???????????????
                    if cycle == Cycle.C1B.value:
                        c1b_family_dict[c.source].append(c.number)
                        continue
                if c.relationship == Relationship.SUGGESTED.value:
                    if cycle not in (Cycle.C2.value, Cycle.C3.value):
                        break
                    if cycle == Cycle.C2.value and fc_count > 10:
                        break
                    if cycle == Cycle.C3.value and fc_count > 20:
                        break
                    fc_count += 1
                numbers.append(c.number)

            # if cycle1 applicant is in no_use add ec
            if len(numbers) == 0 or not call_history:
                src_contact = (
                    Contact.select()
                    .where(Contact.user_id == application.user_id,
                           Contact.source in FamilyContactType.c1a_order()))

                # C1A??????????????????????????????,?????????????????????;????????????2????????????4???
                c1a_family_dict = defaultdict(list)
                for e in src_contact:
                    c1a_family_dict[e.source].append(e.number)

                for call_type in FamilyContactType.c1a_order():
                    numbers.extend(c1a_family_dict[call_type])

            if cycle == Cycle.C1B.value:
                for call_type in FamilyContactType.c1b_order():
                    numbers.extend(c1b_family_dict[call_type])

            numbers = list(set(numbers))
            update_query = (
                AutoCallList
                .update(numbers=','.join(numbers))
                .where(AutoCallList.application == application.id)
            )
            update_query.execute()


def check_valid_phone(phone_list, contact):
    useful = ContactsUseful.AVAILABLE.value
    for each_phone in phone_list:
        if contact.number == each_phone.get('tel_no') or \
                contact.number == each_phone.get('mobile_no'):
            useful = ContactsUseful.INVALID.value
            break
    return useful

# c1a????????????5?????????????????????,??????ec
def check_call_history(application):
    app_create_at = application.created_at + timedelta(days=4)
    if datetime.today().date() > app_create_at.date():
        call_actions = (CallActions.select()
                        .where(CallActions.type == 0,
                               CallActions.application == application.id,
                               CallActions.created_at >
                               (datetime.now() - timedelta(days=5))))
        for call in call_actions:
            if call.phone_status == PhoneStatus.CONNECTED.value:
                return True
        return False
    return True


#??????????????????????????????????????? SCAVENGER_TIME ????????????SCAVENGER????????????
@action(MessageAction.BOMBER_SCAVENGER)
def scavenger(payload, msg_id):
    scavenger_time = -60
    scavenger = (SystemConfig.select()
                 .where(SystemConfig.key == 'SCAVENGER_TIME')
                 .first())
    if scavenger and scavenger.value.isdigit():
        scavenger_time = -int(scavenger.value)
    update_auto_call_list = (
        AutoCallList
        .update(status=AutoListStatus.PENDING.value,
                description='scavenger')
        .where(
            AutoCallList.status == AutoListStatus.PROCESSING.value,
            AutoCallList.updated_at <
            datetime.now() + timedelta(minutes=scavenger_time),
        )
    )
    count = update_auto_call_list.execute()
    logging.info('scavenger processed %s application', count)

    # ???????????????????????????????????????????????????
    mail_box_scavenger_time = -30
    mail_box_scavenger = (SystemConfig.select()
                          .where(SystemConfig.key == 'MAIL_BOX_SCAVENGER_TIME')
                          .first())
    if mail_box_scavenger and mail_box_scavenger.value.isdigit():
        mail_box_scavenger_time = -int(mail_box_scavenger.value)
    update_mail_box_call_list = (
        AutoCallList.update(status=AutoListStatus.PENDING.value)
        .where(AutoCallList.status == AutoListStatus.MAILBOX.value,
               AutoCallList.updated_at <
               datetime.now() + timedelta(minutes=mail_box_scavenger_time))
    )
    mail_box_count = update_mail_box_call_list.execute()
    logging.info("scavenger update mail box %s", mail_box_count)

    # ivr???30????????????????????????????????????ivr????????????
    update_auto_ivr = (
        AutoIVR
            .update(status=AutoIVRStatus.AVAILABLE.value)
            .where(AutoIVR.status == AutoIVRStatus.PROCESSING.value,
                   AutoIVR.updated_at < datetime.now() + timedelta(minutes=-30)
            )
    )
    ivr_result = update_auto_ivr.execute()
    logging.info("scavenger update %s ivr"%ivr_result)


@action(MessageAction.BOMBER_CLEAR_OVERDUE_PTP)
def bomber_clear_overdue_ptp(payload, msg_id):
    # ??????C1B, C2 ??? C3 ?????????????????????????????????ptp???????????????????????????ab_test
    #C1B, C2,C3 ??????????????????????????????????????????????????????????????????
    update_overdue_ptp_ab = (
        Application.update(
            status=ApplicationStatus.AB_TEST.value,
        ).where(
            fn.DATE(Application.promised_date) < datetime.today().date(),
            Application.status == ApplicationStatus.PROCESSING.value,
            Application.cycle << [Cycle.C1B.value, Cycle.C2.value, Cycle.C3.value]
        )
    )
    count1 = update_overdue_ptp_ab.execute()
    logging.info('bomber overdue ptp for C1B C2 and C3 cleared: %s', count1)

    now_and_yesterday = ((datetime.today() + timedelta(days=1)).date(),
                         datetime.today().date())
    overdue_1a1b_cs_ptp = (CallActions
                           .select()
                           .where(fn.DATE(CallActions.promised_date)
                                  .in_(now_and_yesterday),
                                  CallActions.bomber_id == 72))
    update_overdue_1a1b_cs_ptp = (
        Application
            .update(status=ApplicationStatus.UNCLAIMED.value)
            .where(Application.status == ApplicationStatus.PROCESSING.value,
                   Application.cycle == Cycle.C1A.value,
                   Application.id.in_(overdue_1a1b_cs_ptp)))

    logging.debug("bomber c1a c1b cs ptp: %s", update_overdue_1a1b_cs_ptp)
    count2 = update_overdue_1a1b_cs_ptp.execute()
    logging.info('bomber c1a c1b cs overdue ptp cleared: %s', count2)

    update_overdue_ptp = (
        Application
        .update(
            status=ApplicationStatus.UNCLAIMED.value,
        ).where(
            fn.DATE(Application.promised_date) < datetime.today().date(),
            Application.status == ApplicationStatus.PROCESSING.value,
            Application.cycle == Cycle.C1A.value,
        )
    )
    count = update_overdue_ptp.execute()
    logging.info('bomber overdue ptp cleared: %s', count)


@action(MessageAction.REPORT_BOMBER_COLLECTION)
def report_bomber_collection(payload, msg_id):
    start_date = (ReportCollection
                  .select(fn.MAX(ReportCollection.apply_date))
                  .scalar())
    now = datetime.now()
    if start_date and str(start_date) == str(now)[:10]:
        return
    end_date = str(now + timedelta(days=1))[:10]
    start_date = str(now)[:10]

    dct = dict(zip(CycleList.sql_values(), CycleList.table_values()))
    all_overdue_loan_sql1 = """
        SELECT ba.cycle, COUNT(ba.id)
        FROM bomber.auto_call_list ba
        GROUP BY 1;
    """
    s_data1 = readonly_db.execute_sql(all_overdue_loan_sql1).fetchall()
    d1 = OperatedDict(s_data1)

    all_overdue_loan_sql2 = """
        SELECT ba.cycle, COUNT(ba.id)
        FROM bomber.auto_call_list ba
        WHERE DATE(ba.follow_up_date) > CURDATE()
        AND ba.called_counts = 0
        GROUP BY 1;
    """
    s_data2 = readonly_db.execute_sql(all_overdue_loan_sql2).fetchall()
    d2 = OperatedDict(s_data2)

    overdue_loans_entered_into_predict_call_system_sql = """
        SELECT ba.cycle, COUNT(ba.id)
        FROM bomber.auto_call_list ba
        WHERE ba.called_counts >= 1
        GROUP BY 1;
    """
    s_data3 = readonly_db.execute_sql(
        overdue_loans_entered_into_predict_call_system_sql).fetchall()
    d3 = OperatedDict(s_data3)

    loans_completed_sql = """
        SELECT ba.cycle, COUNT(DISTINCT ba.application_id)
        FROM bomber.auto_call_actions ba
        WHERE DATE(ba.created_at) = CURDATE()
        GROUP BY 1;
    """
    s_data4 = readonly_db.execute_sql(loans_completed_sql).fetchall()
    d4 = OperatedDict(s_data4)

    connected_calls_automatic_sql = """
        SELECT ba.cycle, COUNT(ba.application_id)
        FROM bomber.auto_call_actions ba
        WHERE DATE(ba.created_at) = CURDATE()
        GROUP BY 1;
    """
    s_data5 = readonly_db.execute_sql(connected_calls_automatic_sql).fetchall()
    d5 = OperatedDict(s_data5)

    connected_calls_manual_sql = """
        SELECT bb.cycle, COUNT(bb.id)
        FROM bomber.bombing_history bb
        WHERE DATE(bb.created_at) = curdate()
        AND (bb.bomber_id < 150 OR bb.bomber_id > 200)
        GROUP BY bb.cycle;
    """
    s_data6 = readonly_db.execute_sql(connected_calls_manual_sql).fetchall()
    d6 = OperatedDict(s_data6)

    logging.info('Directly get data from database successfully.')

    c1 = d1 - d2
    c2 = d3
    c3 = c2 / c1
    c4 = d4
    c5 = c4 / c2
    c6 = d5
    c7 = c6 / c4
    c8 = d6
    c9 = OperatedDict(get_agent())
    c10 = (c6 + c8) / c9
    try:
        c11 = average_call_duration_team(start_date, end_date)
    except AttributeError:
        c11 = {}
    lst = []
    for i in range(1, 5):
        lst.append({
            'apply_date': start_date,
            'cycle': dct[i],
            'all_overdue_loan': c1.get(i, 0),
            'overdue_loans_entered_into_predict_call_system': c2.get(i, 0),
            'of_overdue_loans_entered_into_predict_call_system':
            round(c3.get(i, 0) * 100, 1),
            'loans_completed': c4.get(i, 0),
            'of_completed_loans_in_predict_call_system':
            round(c5.get(i, 0) * 100, 1),
            'connected_calls_automatic': c6.get(i, 0),
            'connected_calls_automatic_completed_loans':
            round(c7.get(i, 0), 1),
            'connected_calls_manual': c8.get(i, 0),
            'agent': c9.get(i, 0),
            'average_calls_agent': round(c10.get(i, 0), 1),
            'average_call_duration_team': round(c11.get(i, 0), 1)
        })
    ReportCollection.insert_many(lst).execute()

    logging.info('report_bomber_collection:Done!')


@action(MessageAction.BOMBER_AUTO_CALL_LIST_RECORD)
def bomber_auto_call_list_record(payload, msg_id):
    """???????????????auto_call_list???????????????????????????????????????????????????"""
    now = datetime.now()
    if now > datetime.strptime('2020-02-01', '%Y-%m-%d'):
        date_sql = """
        SELECT DATE(created_at) FROM auto_call_list_record
        GROUP BY DATE(created_at) limit 1
        """
        del_date = db.execute_sql(date_sql).fetchone()[0]
        del_sql = """
        DELETE FROM auto_call_list_record WHERE date(created_at) = %s
        """
        db.execute_sql(del_sql, [del_date])
    sql = """
    INSERT INTO auto_call_list_record
    SELECT * FROM auto_call_list
    """
    db.execute_sql(sql)
    logging.info("bomber_auto_call_list_record done")


@action(MessageAction.BOMBER_MANUAL_CALL_LIST)
def bomber_manual_call_list(payload, msg_id):
    """
    ????????????????????????

    :param payload:
    :param msg_id:
    :return:
    """
    batch_id = payload.get('batch_id')
    if batch_id is None:
        logging.warning('Invalid batch id')
        return
    query = (ManualCallList
             .select()
             .where(ManualCallList.batch_id == batch_id,
                    ManualCallList.status << ManualCallListStatus.available()))
    if not query.exists():
        logging.warning('Empty application id list')
        return

    for q in query:
        application_ids = json.loads(q.application_ids or '[]')

        # where
        cycle = 0
        where_list = [(Application.id << application_ids),
                      Application.latest_bomber_id == q.src_bomber_id]
        src_params = json.loads(q.src_params or '{}')
        if "cycle" in src_params:
            where_list.append(Application.cycle == src_params['cycle'])
            cycle = src_params['cycle']
        if "status" in src_params:
            where_list.append(Application.status == src_params['status'])

        # update
        update_dict = {'latest_bomber': q.dest_bomber_id}
        dest_params = json.loads(q.dest_params or '{}')
        if "cycle" in dest_params:
            update_dict['cycle'] = dest_params['cycle']
            cycle = dest_params['cycle']
        if "status" in dest_params:
            update_dict['status'] = dest_params['status']

        with db.atomic():
            try:
                # update dispatch_app
                if q.update_dispatch_app:
                    if q.dest_partner_id is None:
                        raise ValueError('unallowed operation')
                    (DispatchApp
                     .delete()
                     .where(DispatchApp.application_id.in_(application_ids))
                     .execute())

                    (DispatchApp
                     .insert_many([{
                        'application': i,
                        'partner': q.dest_partner_id,
                        'bomber': q.dest_bomber_id,
                        'status': DisAppStatus.NORMAL.value}
                        for i in application_ids])
                     .execute())
            
                application_success_row = (
                    Application
                    .update(**update_dict)
                    .where(*where_list)
                    .execute()
                )
                if application_success_row == 0:
                    raise ValueError('Invalid parameter')

                (ManualCallList
                 .update(
                     status=ManualCallListStatus.SUCCESS.value,
                     length=application_success_row)
                 .where(ManualCallList.id == q.id)
                 .execute())

                out_and_in_record(
                    src_bomber_id=q.src_bomber_id,
                    application_ids=application_ids,
                    dest_partner_id=q.dest_partner_id,
                    dest_bomber_id=q.dest_bomber_id,
                    cycle=cycle
                )
            except Exception:
                db.rollback()
                (ManualCallList
                 .update(
                     status=ManualCallListStatus.FAILED.value,
                     length=0)
                 .where(ManualCallList.id == q.id)
                 .execute())
                logging.error("PRINT BOMBER_MANUAL_CALL_LIST ERROR:\n%s",
                              traceback.format_exc())
                continue


def lambda_result(item, dct):
    a = str(item.application_id)
    entry_principal_pending = (Decimal(item.amount or 0) -
                               dct[a]['principal_paid'])
    entry_late_fee_pending = dct[a]['late_fee'] - dct[a]['late_fee_paid']

    return {
        'created_at': item.created_at,
        'updated_at': item.updated_at,
        'application': a,
        'bomber_id': item.bomber_id,
        'entry_at': item.entry_at,
        'entry_overdue_days': item.entry_overdue_days,
        'partner_id': item.partner_id,
        'expected_out_time': item.expected_out_time,
        'entry_principal_pending': entry_principal_pending,
        'entry_late_fee_pending': entry_late_fee_pending
    }


def out_and_in_record(**kwargs):
    """
    ????????????????????????????????????
    """
    new_out_record(**kwargs)
    new_in_record(**kwargs)

def new_out_record(**kwargs):
    if not kwargs['application_ids']:
        return
    (DispatchAppHistory
     .update(out_at=fn.NOW())
     .where(DispatchAppHistory.bomber_id == kwargs['src_bomber_id'],
            DispatchAppHistory.application << kwargs['application_ids'],
            DispatchAppHistory.out_at.is_null(True))
     .execute())
    # ????????????????????????ptp_bomber????????????
    if kwargs.get("month_dispatch"):
        return
    # ????????????p??????ptp_bomber?????????
    try:
        (Application.update(ptp_bomber=None)
                    .where(Application.id << kwargs["application_ids"])
                    .execute())
    except Exception as e:
        logging.error("new_out_record error:aids:%s,error:%s" %
                      (kwargs["application_ids"],str(e)))

def new_in_record(**kwargs):
    cycle_period = {
        1: '10',
        2: '30',
        3: '60',
        4: '90'
    }
    period = cycle_period.get(kwargs['cycle'], '90 + t1.overdue_days')
    kwargs['dest_partner_id'] = kwargs.get('dest_partner_id') or 'null'
    subquery = (Application
                .select(Application.amount,
                        fn.NOW().alias('created_at'),
                        fn.NOW().alias('updated_at'),
                        Application.id.alias('application_id'),
                        R(str(kwargs['dest_bomber_id'])).alias('bomber_id'),
                        fn.NOW().alias('entry_at'),
                        Application.overdue_days.alias('entry_overdue_days'),
                        R(str(kwargs['dest_partner_id'])).alias('partner_id'),
                        (SQL('DATE_ADD(CURDATE(),INTERVAL (%s -'
                             ' t1.overdue_days) DAY)' % period))
                        .alias('expected_out_time'))
                .where(Application.status != ApplicationStatus.REPAID.value,
                       Application.id << kwargs['application_ids']))
    application_list = list(subquery)
    for idx in range(0, len(application_list), 1000):
        applications = application_list[idx:idx + 1000]
        app_ids = [i.application_id for i in applications]
        bill_list = BillService().bill_list(application_ids=app_ids)
        bill_dict = {str(bill['application_id']): bill for bill in bill_list}
        insert_args = list(map(partial(lambda_result,
                                       dct=bill_dict),
                               applications))
        DispatchAppHistory.insert_many(insert_args).execute()



def end_old_application(old_app, paid=False):
    if paid:
        if old_app.status == OldLoanStatus.WAITING.value:
            old_app.status = OldLoanStatus.PAID.value
            old_app.save()
            return
        if old_app.status == OldLoanStatus.PROCESSING.value:
            old_app.status = OldLoanStatus.PAID.value
            old_app.save()
            return old_app.application_id

    end_date = old_app.end_date
    now = datetime.now()
    if now >= max(end_date, old_app.promised_date or now):
        old_app.status = OldLoanStatus.FINISHED.value
        old_app.save()
        return old_app.application_id


@action(MessageAction.UPDATE_OLD_LOAN_APPLICATION)
def update_old_loan_application(payload, msg_id):
    items = (Application
             .select(Application, OldLoanApplication)
             .join(OldLoanApplication,
                   JOIN_INNER,
                   on=(Application.id ==
                       OldLoanApplication.application_id).alias('old_app'))
             .where(OldLoanApplication.status
                    .in_(OldLoanStatus.available())))
    out_list = []
    for application in items:
        if application.overdue_days > 90:
            if application.old_app.status == OldLoanStatus.WAITING.value:
                start_old_application(application.old_app)
            else:
                out_list.append(application.old_app)

    success_list = [end_old_application(item) for item in out_list]
    app_ids = list(filter(None, success_list))

    if app_ids:
        bomber_id = SpecialBomber.OLD_APP_BOMBER.value
        out_record(src_bomber_id=bomber_id, application_ids=app_ids)


def in_record(**kwargs):
    """
    :param kwargs: dist_partner_id, dist_bomber_id,
     expected_out_time, application_ids
    :return:
    """
    # TODO: ??????????????????
    kwargs['dist_partner_id'] = kwargs.get('dist_partner_id') or 'null'
    subquery = (Application
                .select(Application.amount,
                        fn.NOW().alias('created_at'),
                        fn.NOW().alias('updated_at'),
                        Application.id.alias('application_id'),
                        R(str(kwargs['dist_bomber_id'])).alias('bomber_id'),
                        fn.NOW().alias('entry_at'),
                        Application.overdue_days.alias('entry_overdue_days'),
                        R(str(kwargs['dist_partner_id'])).alias('partner_id'),
                        R('"{}"'.format(kwargs['expected_out_time']))
                        .alias('expected_out_time'))
                .where(Application.status != ApplicationStatus.REPAID.value,
                       Application.id << kwargs['application_ids']))
    application_list = list(subquery)
    for idx in range(0, len(application_list), 1000):
        applications = application_list[idx:idx + 1000]
        app_ids = [i.application_id for i in applications]
        bill_list = BillService().bill_list(application_ids=app_ids)
        bill_dict = {str(bill['application_id']): bill for bill in bill_list}
        insert_args = list(map(partial(lambda_result, dct=bill_dict),
                               applications))
        DispatchAppHistory.insert_many(insert_args).execute()


def out_record(**kwargs):
    """

    :param kwargs: src_bomber_id, application_ids
    :return:
    """
    # TODO: ??????????????????
    if not kwargs.get('application_ids'):
        return
    (DispatchAppHistory
     .update(out_at=fn.NOW())
     .where(DispatchAppHistory.bomber_id == kwargs['src_bomber_id'],
            DispatchAppHistory.application << kwargs['application_ids'])
     .execute())
    # ????????????p??????ptp_bomber?????????
    try:
        (Application.update(ptp_bomber=None)
         .where(Application.id << kwargs["application_ids"])
         .execute())
    except Exception as e:
        logging.error("out_record error:aids:%s,error:%s" %
                      (kwargs["application_ids"], str(e)))


def start_old_application(old_app, cancel=False):
    application_id = old_app.application_id
    if cancel and (old_app.status == OldLoanStatus.PAID.value):
        now = datetime.now()
        if old_app.start_date is None:
            # ?????????500????????????
            old_app.status = OldLoanStatus.WAITING.value
        elif now >= max(old_app.end_date, old_app.promised_date or now):
            # ????????????????????????500???????????????
            old_app.status = OldLoanStatus.FINISHED.value
            (DispatchAppHistory
             .update(out_at=max(old_app.end_date,
                                old_app.promised_date or now))
             .where(DispatchAppHistory.bomber_id == old_app.bomber_id,
                    DispatchAppHistory.application == application_id)
             .execute())
        else:
            # ??????500?????????
            old_app.status = OldLoanStatus.PROCESSING.value
            (DispatchAppHistory
             .update(out_at=None)
             .where(DispatchAppHistory.bomber_id == old_app.bomber_id,
                    DispatchAppHistory.application == application_id)
             .execute())
        old_app.save()
        return

    application = (
        Application
            .get_or_none(Application.id == application_id,
                         Application.status != ApplicationStatus.REPAID.value,
                         Application.overdue_days > 90,
                         Application.promised_date.is_null(True) |
                         (fn.DATE(Application.promised_date) <
                          datetime.today().date())))
    if not application:
        logging.error("Can not set old application %s to start collecting",
                      application_id)
        return

    if old_app.status in OldLoanStatus.no_available():
        logging.info("%s has finished or paid", old_app.application_id)
        return

    config = SystemConfig.prefetch(SCI.OLD_APP_PERIOD)
    sp = config.get(SCI.OLD_APP_PERIOD,
                    SCI.OLD_APP_PERIOD.default_value)
    old_app_bomber = SpecialBomber.OLD_APP_BOMBER.value
    old_app.status = OldLoanStatus.PROCESSING.value
    old_app.bomber_id = old_app_bomber
    old_app.start_date = datetime.now()
    # ??????????????????end_date????????????????????????
    if not old_app.end_date:
        old_app.end_date = datetime.now() + timedelta(days=sp)
    old_app.save()
    in_record(dist_partner_id=None, dist_bomber_id=old_app_bomber,
              application_ids=[old_app.application_id],
              expected_out_time=str(old_app.end_date))


@action(MessageAction.OLD_LOAN_APPLICATION)
def old_loan_application(payload, msg_id):
    application_id = payload.get('application_id')
    numbers = payload.get('numbers', [])
    if not (application_id and numbers):
        logging.error("empty application id: %s, or invalid numbers: %s",
                      application_id, numbers)

    application = Application.get_or_none(Application.id == application_id)
    if (application and
            application.status == ApplicationStatus.REPAID.value):
        logging.error("application %s has paid", application_id)
        return

    gold_eye = GoldenEye().get('/applications/%s' % application_id)
    if not gold_eye.ok:
        raise RuntimeError('Get golden eye user failed. {}'
                           .format(str(application_id)))

    gold_app = gold_eye.json().get('data')
    user_id = gold_app['user_id']
    user_name = gold_app['id_name']

    # ??????bill??????????????????,?????????????????????????????????OldloanApplication
    try:
        bill = BillService().bill_dict(application_id=application_id)
    except Exception:
        logging.error(
            'application %s get bill info failed,old_loan_application',
            application_id)
        return

    source_contacts = (Contact
                       .filter(Contact.user_id == user_id,
                               Contact.relationship ==
                               Relationship.APPLICANT.value,
                               Contact.source ==
                               ApplicantSource.NEW_APPLICANT.value))
    source_contact_set = {i.number for i in source_contacts}

    # ?????????????????????????????????
    if bill["category"] != ApplicationType.CASH_LOAN_STAGING.value:
        # ????????????new applicant??????
        old_app = OldLoanApplication.get_or_none(
            OldLoanApplication.application_id == application_id,
            OldLoanApplication.status.in_(OldLoanStatus.available())
        )
        if not old_app:
            old_app = OldLoanApplication.create(application_id=application_id,
                                                user_id=user_id,
                                                numbers=','.join(numbers))
        else:
            _numbers = old_app.numbers.split(',')
            # ???????????????????????????
            old_app.numbers = ','.join(set([nu for nu in (_numbers + numbers)
                                            if nu]))
        # ????????????end_date + 7
        if old_app.status == OldLoanStatus.PROCESSING.value:
            old_app.end_date = old_app.end_date + timedelta(days=7)
        old_app.save()

    new_contact = set(numbers) - source_contact_set
    insert_args = [{'user_id': user_id,
                    'name': user_name,
                    'number': i,
                    'relationship': Relationship.APPLICANT.value,
                    'source': ApplicantSource.NEW_APPLICANT.value,
                    'real_relationship': Relationship.APPLICANT.value
                    } for i in new_contact]
    if insert_args:
        Contact.insert_many(insert_args).execute()
    if bill["category"] == ApplicationType.CASH_LOAN_STAGING.value:
        return
    start_old_application(old_app)


def run_one_sql(sql):
    try:
        cursor = readonly_db.get_cursor()
        cursor.execute(sql)
        result = cursor.fetchone()[0] / 1000000
    except Exception as e:
        logging.info('run sql error: %s' % str(sql))
        result = Decimal(0)
    return result


def run_member_sql(sql):
    result = [0, 0]
    try:
        cursor = readonly_db.get_cursor()
        cursor.execute(sql)
        sql_result = cursor.fetchone()
        if sql_result:
            result = sql_result
    except Exception as e:
        logging.info('run sql error: %s' % str(sql))
    return result


def run_all_sql(sql):
    try:
        cursor = readonly_db.get_cursor()
        cursor.execute(sql)
        result = cursor.fetchall()
    except Exception as e:
        logging.info('run sql error: %s' % str(sql))
        result = []
    return result


# ??????dpd1-3???????????????recover_rate(??????)
def get_before_bomber(date_time):
    begin_time = str(date_time - timedelta(days=7))
    end_time = str(date_time)
    # ?????????????????????????????????????????????
    old_sql = """
        select 
            sum(principal_pending+late_fee_pending+interest_pending) as amount
        from 
            bill_java.overdue bb
        where 
            created_at>'%s' 
            and created_at<date_add('%s',interval 1 day)
            and overdue_days in (2,3)
    """ % (begin_time, begin_time)
    old_data = run_one_sql(old_sql)

    # ?????????????????????dpd1?????????????????????
    new_sql = """
        select 
            sum(principal_pending+late_fee_pending+interest_pending) as amount
        from 
            bill_java.overdue bb
        where 
            created_at> '%s' 
            and created_at<'%s'
            and overdue_days=1;
    """ % (begin_time, end_time)
    new_data = run_one_sql(new_sql)

    # ??????????????????dpd4?????????
    dpd4_sql = """
        select 
            sum(principal_pending+late_fee_pending+interest_pending) as amount
        from 
            bill_java.overdue bb
        where 
            created_at>date_add('%s',interval 1 day) 
            and created_at< date_add('%s',interval 1 day)
            and overdue_days=4;
    """ % (begin_time, end_time)
    dpd4_data = run_one_sql(dpd4_sql)

    # ????????????dpd2\3??????
    dpd2_sql = """
        select 
            sum(principal_pending+late_fee_pending+interest_pending) as amount
        from 
            bill_java.overdue bb
        where 
            created_at>'%s' 
            and created_at< date_add('%s',interval 1 day)
            and overdue_days in (2,3)
    """ % (end_time, end_time)
    dpd2_data = run_one_sql(dpd2_sql)

    all_money = old_data + new_data
    repayment = all_money - dpd4_data - dpd2_data
    pro = 0
    if all_money:
        pro = (repayment / all_money) * 100
    RepaymentReport.create(
        time=begin_time,
        cycle=0,
        all_money=all_money,
        proportion=pro,
        repayment=repayment
    )


# ??????????????????recover_rate????????????(????????????)
@action(MessageAction.RECOVER_RATE_WEEK_MONEY)
def recover_rate_week_money(payload, msg_id):
    #????????????RECOVER_RATE_WEEK_MONEY????????????
    worker_log = (WorkerLog.select(fn.COUNT(WorkerLog.action).alias('logs'))
                  .where(WorkerLog.created_at >= date.today(),
                         WorkerLog.action == 'RECOVER_RATE_WEEK_MONEY')
                  .first())

    if worker_log.logs >= 5:
        return
    logging.info('start cal recover_rate_week_money')
    date_time = date.today()
    get_every_cycle_report(date_time)


# ?????????????????????dpd1-3???recover_rate
def get_before_bomber_rate(date_time):
    begin_time = date_time - timedelta(days=1)
    end_time = date_time

    for is_first_loan in FIRSTLOAN.values():
        begin_date = begin_time
        end_date = end_time
        for i in range(2, 5):
            money_sql = """
                select 
                    sum(bo1.principal_pending+bo1.late_fee_pending+
                        bo1.interest_pending) as dpd1_pending, 
                    sum(bo2.principal_pending+bo2.late_fee_pending+
                        bo2.interest_pending) as dpd4_pending
                from bill_java.overdue bo1
                    left join dashboard.application da 
                      on bo1.application_id=da.id 
                    left join bill_java.overdue bo2 
                      on bo1.application_id=bo2.application_id 
                      and bo2.overdue_days=%s and bo2.status = 1
                where bo1.overdue_days=1 
                  and bo1.status = 1
                  and bo1.which_day_overdue>='%s' 
                  and bo1.which_day_overdue<'%s'
                  and da.is_first_loan = %s
                  and bo1.stage_num is null
            """ % (i, begin_date, end_date, is_first_loan)
            try:
                cursor = readonly_db.get_cursor()
                cursor.execute(money_sql)
                money = cursor.fetchone()
                all_money = money[0] / 1000000
                dpd4_money = money[1] / 1000000
            except Exception as e:
                logging.info('get all_money error: %s' % str(e))
                all_money = 0
                dpd4_money = 0

            repayment = all_money - dpd4_money
            if begin_date == date_time - timedelta(days=1):
                RepaymentReportInto.create(
                    time=begin_date,
                    cycle=0,
                    all_money=round(all_money, 3),
                    proportion='0',
                    repayment=round(repayment, 3),
                    is_first_loan=is_first_loan,
                    contain_out=ContainOut.CONTAIN.value
                )
            else:
                pro = '0'
                if all_money:
                    pro = (repayment / all_money) * 100
                    pro = str(round(pro, 2))
                RepaymentReportInto.update(
                    repayment=round(repayment, 3),
                    proportion=pro
                ).where(
                    RepaymentReportInto.time == begin_date,
                    RepaymentReportInto.cycle == 0,
                    RepaymentReportInto.is_first_loan == is_first_loan
                ).execute()

            end_date = begin_date
            begin_date = begin_date - timedelta(days=1)


# ??????c1a???????????????recover_rate
def get_c1a_into_rate(date_time):
    begin_time = date_time - timedelta(days=1)
    end_time = date_time

    for is_first_loan in FIRSTLOAN.values():
        begin_date = begin_time
        end_date = end_time
        all_money_sql = """
            select sum(o.principal_pending+o.late_fee_pending+
                   o.interest_pending) as pending_amount  
            from (
            select ba.id as application_id,ba.C1A_entry as cdt
             from bomber.application ba
            left join dashboard.application da on ba.id=da.id 
            where ba.C1A_entry >= '%s'
            and ba.C1A_entry < '%s'
            and ba.type = 0
            and da.is_first_loan = %s
            )  a
            inner join bill_java.overdue o 
               on a.application_id=o.application_id 
               and date(a.cdt)=date(o.created_at) 
        """ % (begin_date, end_date, is_first_loan)
        all_money = run_one_sql(all_money_sql)

        begin_date = date_time - timedelta(days=19)
        repayment_sql = """
            select 
                sum(b.principal_part+b.late_fee_part) as paid_amount,  
                cdt
            from 
                (select 
                        br.principal_part, br.late_fee_part,   
                        date(cdt) as cdt, br.repay_at, br.application_id
                from (
                        select ba.id, ba.C1A_entry as cdt
                        from bomber.application ba
                        left join dashboard.application da on ba.id=da.id
                        where ba.C1A_entry >= '%s'
                            and ba.C1A_entry < '%s'
                            and ba.type = 0
                            and da.is_first_loan = %s
                        )  a
                left join bomber.repayment_log br on br.application_id = a.id 
                        and br.cycle = 1 and date(br.repay_at) >= date(a.cdt)
                group by 4, 5) b
            group by 2
            """ % (begin_date, end_date, is_first_loan)
        repayment = run_all_sql(repayment_sql)

        if not repayment:
            return
        RepaymentReportInto.create(
            time=end_date - timedelta(days=1),
            cycle=Cycle.C1A.value,
            all_money=round(all_money, 3),
            proportion='0',
            repayment=0,
            is_first_loan=is_first_loan,
            contain_out=ContainOut.CONTAIN.value
        )

        for d in repayment:
            repay = d[0] / 1000000
            report = RepaymentReportInto.filter(
                RepaymentReportInto.time == d[1],
                RepaymentReportInto.cycle == Cycle.C1A.value,
                RepaymentReportInto.is_first_loan == is_first_loan
            ).first()
            if report:
                report.repayment = round(repay, 3)
                pro = (repay / report.all_money) * 100
                pro = str(round(pro, 2))
                report.proportion = pro
                report.save()


# ??????c1b???????????????recover_rate
def get_c1b_into_rate(date_time):
    begin_time = date_time - timedelta(days=1)
    end_time = date_time
    for is_first_loan in FIRSTLOAN.values():
        begin_date = begin_time
        end_date = end_time
        all_money_sql = """
            select sum(o.principal_pending+o.late_fee_pending+
                       o.interest_pending) as pending_amount 
            from (
            select ba.id as application_id,c1b_entry as cdt
            from bomber.application ba
            left join dashboard.application da on ba.id=da.id 
            where ba.c1b_entry >= '%s'
            and ba.c1b_entry < '%s'
            and ba.type = 0
            and da.is_first_loan = %s
            )  a
            inner join bill_java.overdue o on a.application_id=o.application_id 
               and date(a.cdt)=date(o.created_at) 
        """ % (begin_date, end_date, is_first_loan)
        all_money = run_one_sql(all_money_sql)

        not_contain_sql = """
            select sum(o.principal_pending+o.late_fee_pending+
                 o.interest_pending) as pending_amount  
            from (
            select ba.id as application_id,c1b_entry as cdt
            from bomber.application ba
            left join dashboard.application da on ba.id=da.id 
            where ba.c1b_entry >= '%s'
            and ba.c1b_entry < '%s'
            and ba.type = 0
            and da.is_first_loan = %s
            and not exists(select 1 from bomber.dispatch_app_history bd 
                           where bd.application_id=ba.id and bd.partner_id=5)
            )  a
            inner join bill_java.overdue o on a.application_id=o.application_id 
               and date(a.cdt)=date(o.created_at) 
        """ % (begin_date, end_date, is_first_loan)
        not_contain_money = run_one_sql(not_contain_sql)

        begin_date = date_time - timedelta(days=22)
        repayment_sql = """
            select sum(b.principal_part+b.late_fee_part) as paid_amount,et
            from 
                (select br.principal_part, br.late_fee_part,
                        date(a.c1b_entry) as et, br.application_id, br.repay_at
                from (
                select ba.id, ba.c1b_entry
                from bomber.application ba
                left join dashboard.application da on ba.id=da.id
                where ba.c1b_entry >= '%s'
                and ba.c1b_entry < '%s'
                and ba.type = 0
                and da.is_first_loan = %s)  a
                left join bomber.repayment_log br on br.application_id = a.id 
                     and br.cycle = 2
                group by 4, 5) b
            group by 2;
        """ % (begin_date, end_date, is_first_loan)
        repayment = run_all_sql(repayment_sql)

        not_contain_repay_sql = """
            select sum(b.principal_part+b.late_fee_part) as paid_amount, b.et
            from
                (select br.principal_part,br.late_fee_part,
                        date(a.c1b_entry) as et, br.application_id, br.repay_at
                from (
                select ba.id, ba.c1b_entry
                from bomber.application ba
                left join dashboard.application da on ba.id=da.id
                where ba.c1b_entry >= '%s'
                and ba.c1b_entry < '%s'
                and ba.type = 0
                and da.is_first_loan = %s
                and not exists(select 1 from bomber.dispatch_app_history bd 
                                             where bd.application_id=ba.id 
                                             and bd.partner_id=5)
                )  a
                left join bomber.repayment_log br on br.application_id = a.id 
                     and br.cycle = 2
                 group by 4, 5) b
            group by 2
        """ % (begin_date, end_date, is_first_loan)
        not_contain_repay = run_all_sql(not_contain_repay_sql)

        if not not_contain_repay and not repayment:
            return
        for i in ContainOut.values():
            if i == ContainOut.NOT_CONTAIN.value:
                RepaymentReportInto.create(
                    time=end_date - timedelta(days=1),
                    cycle=Cycle.C1B.value,
                    all_money=round(not_contain_money, 3),
                    proportion='0',
                    repayment=0,
                    is_first_loan=is_first_loan,
                    contain_out=ContainOut.NOT_CONTAIN.value
                )
                for repay in not_contain_repay:
                    repay_money = 0
                    if repay[0]:
                        repay_money = repay[0] / 1000000

                    report = RepaymentReportInto.filter(
                        RepaymentReportInto.time == repay[1],
                        RepaymentReportInto.is_first_loan == is_first_loan,
                        RepaymentReportInto.contain_out == i,
                        RepaymentReportInto.cycle == Cycle.C1B.value
                    ).first()
                    if report and report.all_money:
                        report.repayment = round(repay_money, 3)
                        pro = (repay_money / report.all_money) * 100
                        pro = str(round(pro, 2))
                        report.proportion = pro
                        report.save()
            elif i == ContainOut.CONTAIN.value:
                RepaymentReportInto.create(
                    time=end_date - timedelta(days=1),
                    cycle=Cycle.C1B.value,
                    all_money=round(all_money, 3),
                    proportion='0',
                    repayment=0,
                    is_first_loan=is_first_loan,
                    contain_out=ContainOut.CONTAIN.value
                )
                for repay in repayment:
                    repay_money = 0
                    if repay[0]:
                        repay_money = repay[0] / 1000000

                    report = RepaymentReportInto.filter(
                        RepaymentReportInto.time == repay[1],
                        RepaymentReportInto.is_first_loan == is_first_loan,
                        RepaymentReportInto.contain_out == i,
                        RepaymentReportInto.cycle == Cycle.C1B.value
                    ).first()
                    if report and report.all_money:
                        report.repayment = round(repay_money, 3)
                        pro = (repay_money / report.all_money) * 100
                        pro = str(round(pro, 2))
                        report.proportion = pro
                        report.save()


# ??????c2???????????????recover_rate
def get_c2_into_rate(date_time):
    begin_time = date_time - timedelta(days=1)
    end_time = date_time
    for is_first_loan in FIRSTLOAN.values():
        begin_date = begin_time
        end_date = end_time
        all_money_sql = """
            select sum(o.principal_pending+o.late_fee_pending+
                       o.interest_pending) as pending_amount 
            from (
            select ba.id,c2_entry as cdt
            from bomber.application ba
            left join dashboard.application da on ba.id=da.id 
            where ba.c2_entry >= '%s'
            and ba.c2_entry < '%s'
            and ba.type = 0
            and da.is_first_loan = %s
            )  a
            inner join bill_java.overdue o 
               on a.id=o.application_id 
               and date(a.cdt)=date(o.created_at) 
        """ % (begin_date, end_date, is_first_loan)
        all_money = run_one_sql(all_money_sql)

        not_contain_sql = """
            select sum(o.principal_pending+o.late_fee_pending+
                 o.interest_pending) as pending_amount  
            from (
            select ba.id,c2_entry as cdt
            from bomber.application ba
            left join dashboard.application da on ba.id=da.id 
            where ba.c2_entry >= '%s'
            and ba.c2_entry < '%s'
            and ba.type = 0
            and da.is_first_loan = %s
            and not exists(select 1 from bomber.dispatch_app_history bd 
                           where bd.application_id=ba.id 
                           and bd.partner_id=1)
            )  a
            inner join bill_java.overdue o on a.id=o.application_id 
               and date(a.cdt)=date(o.created_at) 
        """ % (begin_date, end_date, is_first_loan)
        not_contain_money = run_one_sql(not_contain_sql)

        begin_date = date_time - timedelta(days=37)
        repayment_sql = """
            select sum(b.principal_part+b.late_fee_part) as paid_amount, b.et
            from
                (select br.principal_part,br.late_fee_part,
                     date(a.c2_entry) as et, br.application_id, br.repay_at
                from (
                select ba.id, ba.c2_entry
                from bomber.application ba
                left join dashboard.application da on ba.id=da.id
                where ba.c2_entry >= '%s'
                and ba.c2_entry < '%s'
                and ba.type = 0
                and da.is_first_loan = %s
                )  a
                left join bomber.repayment_log br on br.application_id = a.id                      
                     and br.cycle = 3
                group by 4, 5) b
            group by 2
        """ % (begin_date, end_date, is_first_loan)
        repayment = run_all_sql(repayment_sql)

        not_contain_repay_sql = """
            select sum(b.principal_part+b.late_fee_part) as paid_amount, b.et
            from
                (select br.principal_part,br.late_fee_part,
                        date(a.c2_entry) as et, br.application_id, br.repay_at
                from (
                select ba.id, ba.c2_entry
                from bomber.application ba
                left join dashboard.application da on ba.id=da.id
                where ba.c2_entry >= '%s'
                and ba.c2_entry < '%s'
                and ba.type = 0
                and da.is_first_loan = %s
                and not exists(select 1 from bomber.dispatch_app_history bd 
                                             where bd.application_id=ba.id 
                                             and bd.partner_id=1)
                )  a
                left join bomber.repayment_log br on br.application_id = a.id 
                    and br.cycle = 3
                group by 4, 5) b
            group by 2
        """ % (begin_date, end_date, is_first_loan)
        not_contain_repay = run_all_sql(not_contain_repay_sql)

        if not not_contain_money and repayment:
            return
        for i in ContainOut.values():
            if i == ContainOut.NOT_CONTAIN.value:
                RepaymentReportInto.create(
                    time=end_date - timedelta(days=1),
                    cycle=Cycle.C2.value,
                    all_money=round(not_contain_money, 3),
                    proportion='0',
                    repayment=0,
                    is_first_loan=is_first_loan,
                    contain_out=ContainOut.NOT_CONTAIN.value
                )
                for repay in not_contain_repay:
                    repay_money = Decimal(0)
                    if repay[0]:
                        repay_money = repay[0]
                    repay_money = repay_money / 1000000
                    report = RepaymentReportInto.filter(
                        RepaymentReportInto.time == repay[1],
                        RepaymentReportInto.is_first_loan == is_first_loan,
                        RepaymentReportInto.contain_out == i,
                        RepaymentReportInto.cycle == Cycle.C2.value
                    ).first()
                    if report and report.all_money:
                        report.repayment = round(repay_money, 3)
                        pro = (repay_money / report.all_money) * 100
                        pro = str(round(pro, 2))
                        report.proportion = pro
                        report.save()
            elif i == ContainOut.CONTAIN.value:
                RepaymentReportInto.create(
                    time=end_date - timedelta(days=1),
                    cycle=Cycle.C2.value,
                    all_money=round(all_money, 3),
                    proportion='0',
                    repayment=0,
                    is_first_loan=is_first_loan,
                    contain_out=ContainOut.CONTAIN.value
                )
                for repay in repayment:
                    repay_money = 0
                    if repay[0]:
                        repay_money = repay[0] / 1000000
                    report = RepaymentReportInto.filter(
                        RepaymentReportInto.time == repay[1],
                        RepaymentReportInto.is_first_loan == is_first_loan,
                        RepaymentReportInto.contain_out == i,
                        RepaymentReportInto.cycle == Cycle.C2.value
                    ).first()
                    if report and report.all_money:
                        report.repayment = round(repay_money, 3)
                        pro = (repay_money / report.all_money) * 100
                        pro = str(round(pro, 2))
                        report.proportion = pro
                        report.save()


# ??????c2???????????????recover_rate
def get_c3_into_rate(date_time):
    begin_time = date_time - timedelta(days=1)
    end_time = date_time

    for is_first_loan in FIRSTLOAN.values():
        begin_date = begin_time
        end_date = end_time
        all_money_sql = """
            select sum(o.principal_pending+o.late_fee_pending+
                       o.interest_pending) as pending_amount 
            from (
            select ba.id, ba.c3_entry as cdt
            from bomber.application ba
            left join dashboard.application da on ba.id=da.id
            where ba.c3_entry >= '%s'
            and ba.c3_entry < '%s'
            and ba.type = 0
            and da.is_first_loan = %s
            )  a
            inner join bill_java.overdue o on a.id=o.application_id 
              and date(a.cdt)=date(o.created_at) 
        """ % (begin_date, end_date, is_first_loan)
        all_money = run_one_sql(all_money_sql)

        begin_date = date_time - timedelta(days=30)
        repayment_sql = """
            select sum(b.principal_part+b.late_fee_part) as paid_amount, b.et
            from
                (select br.principal_part,br.late_fee_part,
                            date(a.c3_entry) as et, br.application_id, br.repay_at
                from (
                select ba.id, ba.c3_entry
                from bomber.application ba
                left join dashboard.application da on ba.id=da.id
                where ba.c3_entry >= '%s'
                and ba.c3_entry < '%s'
                and ba.type = 0
                and da.is_first_loan = '%s'
                )  a
                left join bomber.repayment_log br on br.application_id = a.id 
                    and br.cycle = 4
                group by 4, 5) b
            group by 2
        """ % (begin_date, end_date, is_first_loan)
        repayment = run_all_sql(repayment_sql)

        RepaymentReportInto.create(
            time=end_date - timedelta(days=1),
            cycle=Cycle.C3.value,
            all_money=round(all_money, 3),
            proportion='0',
            repayment=0,
            is_first_loan=is_first_loan,
            contain_out=ContainOut.CONTAIN.value
        )
        if not repayment:
            return
        for repay in repayment:
            repay_money = Decimal(0)
            if repay[0]:
                repay_money = repay[0]
            repay_money = repay_money / 1000000
            report = RepaymentReportInto.filter(
                RepaymentReportInto.time == repay[1],
                RepaymentReportInto.cycle == Cycle.C3.value,
                RepaymentReportInto.is_first_loan == is_first_loan
            ).first()
            if report:
                report.repayment = repay_money
                pro = 0
                if report.all_money and int(report.all_money):
                    pro = (repay_money / report.all_money) * 100
                pro = str(round(pro, 2))
                report.proportion = pro
                report.save()


# ??????????????????recover_rate????????????(????????????)
@action(MessageAction.RECOVER_RATE_WEEK_MONEY_INTO)
def recover_rate_week_money_into(payload, msg_id):
    worker_log = (WorkerLog.select(fn.COUNT(WorkerLog.action).alias('logs'))
                  .where(WorkerLog.created_at >= date.today(),
                         WorkerLog.action == 'RECOVER_RATE_WEEK_MONEY_INTO')
                  .first())

    if worker_log and worker_log.logs >= 5:
        return
    date_time = date.today()
    get_before_bomber_rate(date_time)
    get_c1a_into_rate(date_time)
    get_c1b_into_rate(date_time)
    get_c2_into_rate(date_time)
    get_c3_into_rate(date_time)

    # ????????????????????????????????????????????????
    ripe_days = {0: 3, 1: 7, 2: 20, 3: 30, 4: 30}
    for i in range(0, 5):
        repe_date = date.today() - timedelta(days=ripe_days[i])
        (RepaymentReportInto
         .update(ripe_ind=RipeInd.RIPE.value)
         .where(RepaymentReportInto.time < repe_date,
                RepaymentReportInto.cycle == i)
         ).execute()


# ----------------- ??????summary_bomber??????summary??????????????? --------------------
# ??????????????????
def get_static_bomber(begin_date):
    active_date = begin_date - timedelta(days=8)
    bombers = (BomberR
               .select(BomberR.id,
                       BomberR.role.alias('role'),
                       BomberR.last_active_at.alias('active'))
               .where(BomberR.last_active_at > active_date,
                      BomberR.role << [1, 2, 4, 5, 6, 8,9]))
    summary = []
    for bomber in bombers:
        summary.append({
            'time': begin_date,
            'bomber_id': bomber.id,
            'cycle': bomber.role.cycle,
            'work_ind': 0
        })
    SummaryBomber.insert_many(summary).execute()


# ??????????????????????????????????????????
@action(MessageAction.SUMMARY_CREATE)
def summary_create(payload, msg_id):
    begin_date = date.today()
    worker_log = (WorkerLog.select(fn.COUNT(WorkerLog.action).alias('logs'))
                  .where(WorkerLog.created_at >= begin_date,
                         WorkerLog.action == 'SUMMARY_CREATE')
                  .first())

    if worker_log and worker_log.logs >= 5:
        return

    get_static_bomber(begin_date)


# ???????????????????????????
def get_active_bomber(begin_date):
    bombers = (BomberR
               .select(BomberR.id)
               .where(BomberR.last_active_at >= begin_date))
    for bomber in bombers:
        (SummaryBomber.update(work_ind=1)
         .where(SummaryBomber.time == begin_date,
                SummaryBomber.bomber_id == bomber.id)
         ).execute()


# ?????????????????????????????????????????????????????????
@time_logger
def get_call_and_made(end_date, begin_date, real_time_query=False):
    call_sql = """
        select 
          bomber_id, 
          count(case when relationship is not null then application_id end) 
                as 'call_cnt', 
          count(distinct case when relationship is not null then 
                application_id end) as 'call_case',
          count(case when phone_status=4 then application_id end) as 'connect',
          count(distinct case when phone_status=4 then application_id end) 
                 as 'connect_case'
        from (
        select bomber_id,application_id,phone_status, cycle, relationship
        from bomber.call_actions ba
        where created_at>'%s' and created_at<'%s'
          and type in (0, 1)
        ) a
        group by 1
    """ % (begin_date, end_date)
    calls = run_all_sql(call_sql)
    if real_time_query:
        return calls
    for call in calls:
        bomber, call_cnt, case_made, connect_cnt, case_connect = call
        (SummaryBomber.update(
            case_made_cnt=case_made,
            call_cnt=call_cnt,
            call_connect_cnt=connect_cnt,
            case_connect_cnt=case_connect)
         .where(
            SummaryBomber.bomber_id == bomber,
            SummaryBomber.time == begin_date)
         ).execute()
    return calls


# ???????????????????????????????????????
@time_logger
def get_claimed_cnt(end_date, begin_date, real_time_query=False):
    table_date = begin_date - timedelta(days=30)
    claimed_sql = """
        SELECT
            COUNT( `t1`.`application_id` ) AS cnt,
            `t1`.`bomber_id` AS bomber_id 
        FROM
            `dispatch_app_history` AS t1 
        WHERE
            ( `t1`.`out_at` >  '%s' OR  `t1`.`out_at` IS null  ) 
            AND ( `t1`.`bomber_id` != 1000 ) 
            AND ( `t1`.`partner_id` IS null ) 
            AND ( `t1`.`entry_at` > '%s' ) 
            AND ( `t1`.`entry_at` < '%s' ) 
        GROUP BY
            `t1`.`bomber_id`
    """ % (begin_date, table_date, end_date)
    claimeds = run_all_sql(claimed_sql)
    if real_time_query:
        return claimeds
    for claimed in claimeds:
        cnt, bomber_id = claimed
        (SummaryBomber.update(claimed_cnt=cnt)
         .where(SummaryBomber.time == begin_date,
                SummaryBomber.bomber_id == bomber_id)
         ).execute()
    return claimeds


# ????????????????????????
def get_sms_data(end_data, begin_data):
    all_sms = (ConnectHistoryR
               .select(ConnectHistoryR.operator.alias('bomber_id'),
                       fn.COUNT(ConnectHistoryR.application).alias('sms_send'))
               .where(ConnectHistoryR.created_at > begin_data,
                      ConnectHistoryR.created_at < end_data,
                      ConnectHistoryR.type.in_(ConnectType.sms()))
               .group_by(ConnectHistoryR.operator))

    for sms in all_sms:
        (SummaryBomber.update(sms_cnt=sms.sms_send)
         .where(SummaryBomber.time == begin_data,
                SummaryBomber.bomber_id == sms.bomber_id)
         ).execute()
    return all_sms


# ??????ptp???????????????
@time_logger
def get_ptp_data(end_date, begin_date, real_query_time=False):
    sql = """
        SELECT
            a.bomber_id,
            sum( a.promised_amount ) AS ptp_amount,
            count( application_id ) 
        FROM
            bomber.auto_call_actions a
            LEFT JOIN bomber.bomber c ON a.bomber_id = c.id 
        WHERE
            a.created_at >= '%s' 
            AND a.created_at < '%s'
            AND a.promised_date != '' 
        GROUP BY 1 
        UNION
        SELECT
            a.bomber_id,
            ifnull( sum( a.promised_amount ), 0 ) AS ptp_amount,
            count( application_id ) 
        FROM
            bomber.bombing_history a
            LEFT JOIN bomber.bomber c ON a.bomber_id = c.id 
        WHERE
            bomber_id NOT BETWEEN 151 
            AND 177 
            AND bomber_id NOT BETWEEN 181 
            AND 183 
            AND bomber_id != 72 
            AND a.created_at >= '%s' 
            AND a.created_at < '%s' 
            AND a.promised_date != '' 
        GROUP BY 1
    """ % (begin_date, end_date, begin_date, end_date)
    ptp_datas = run_all_sql(sql)
    if real_query_time:
        return ptp_datas

    result = {}
    for ptp in ptp_datas:
        bomber_id, amount, cnt = ptp
        if bomber_id in result.keys():
            result[bomber_id][0] += amount
            result[bomber_id][1] += cnt
            continue
        result[bomber_id] = [amount, cnt]
    for key, value in result.items():
        (SummaryBomber
         .update(
            promised_cnt=value[1],
            promised_amount=value[0]
         ).where(
            SummaryBomber.time == begin_date,
            SummaryBomber.bomber_id == key
         )).execute()
    return ptp_datas


# ?????????????????????????????????
@time_logger
def get_recover_amount(end_date, begin_date, real_time_query=False):
    C1_sql = """
        SELECT a.current_bomber_id,
               sum(principal_part+late_fee_part) as pay_amount,
               count(distinct application_id)
        from 
            (select a.cycle,a.current_bomber_id,b.username,a.principal_part,
                    a.late_fee_part,a.application_id,a.repay_at
            FROM bomber.repayment_log a ,bomber.bomber b
            WHERE a.repay_at >= '%s' AND a.repay_at <'%s'
            AND a.current_bomber_id !=''
            AND a.current_bomber_id = b.id
            and b.role_id in (1,4)
            and principal_part+late_fee_part>0
            group by 6,7) a
        GROUP BY a.cycle,a.current_bomber_id
    """ % (begin_date, end_date)
    C1_results = run_all_sql(C1_sql)
    if not real_time_query:
        for C1_result in C1_results:
            bomber_id, amount, cnt = C1_result
            (SummaryBomber.update(
                cleared_cnt=cnt,
                cleared_amount=amount
            ).where(
                SummaryBomber.bomber_id == bomber_id,
                SummaryBomber.time == begin_date
            )).execute()

    other_sql = """
        select current_bomber_id,sum(pay_amount) as pay_amount,
               count(distinct application_id)
        from (
        select application_id,current_bomber_id,pay_amount,repay_at
        from (
        select br.application_id,br.current_bomber_id,
               br.principal_part+br.late_fee_part as pay_amount,br.repay_at
                     from bomber.repayment_log br
                     left join bomber.bomber bb on br.current_bomber_id=bb.id
        where exists (select 1 from bomber.bombing_history bb 
                      where br.current_bomber_id=bb.bomber_id 
                        and br.application_id=bb.application_id 
                        and bb.created_at<br.repay_at 
                        and (bb.promised_date is not null 
                             or bb.promised_amount is not null))
        and br.repay_at >= '%s'
        and br.repay_at < '%s'
        and bb.role_id in (2,3,5,6,7,8,9) 
        and br.principal_part+br.late_fee_part > 0
        group by 1,4
        ) a
        group by 1,4) b
        group by 1
    """ % (begin_date, end_date)
    sql_results = run_all_sql(other_sql)
    if not real_time_query:
        for sql_result in sql_results:
            bomber_id, amount, cnt = sql_result
            (SummaryBomber.update(
                cleared_cnt=cnt,
                cleared_amount=amount
            ).where(
                SummaryBomber.bomber_id == bomber_id,
                SummaryBomber.time == begin_date
            )).execute()
    result = sql_results + C1_results
    return result


# summary ???????????????(???????????????????????????????????????)
@action(MessageAction.SUMMARY_NEW)
def summary_new(payload, msg_id):
    end_date = date.today()
    begin_date = end_date - timedelta(days=1)
    worker_log = (WorkerLog.select(fn.COUNT(WorkerLog.action).alias('logs'))
                  .where(WorkerLog.created_at >= end_date,
                         WorkerLog.action == 'SUMMARY_NEW')
                  .first())

    if worker_log and worker_log.logs >= 5:
        return

    get_active_bomber(begin_date)
    get_call_and_made(end_date, begin_date)
    get_claimed_cnt(end_date, begin_date)
    get_sms_data(end_date, begin_date)
    get_ptp_data(end_date, begin_date)
    get_recover_amount(end_date, begin_date)
    get_unfollowed(begin_date)
    get_unfollowed_call(begin_date)


# ------------------------ ??????summary bomber?????????????????? ----------------------
# ???????????????????????????
def get_new_case_amount(begin_date, end_date):
    all_case = (DispatchAppHistoryR
                .select(fn.SUM(DispatchAppHistoryR.entry_late_fee_pending +
                               DispatchAppHistoryR.entry_principal_pending)
                        .alias('pending'),
                        DispatchAppHistoryR.bomber_id,
                        fn.COUNT(DispatchAppHistoryR.application).alias('cnt'))
                .where(DispatchAppHistoryR.entry_at > begin_date,
                       DispatchAppHistoryR.entry_at < end_date,
                       DispatchAppHistoryR.partner_id.is_null(True))
                .group_by(DispatchAppHistoryR.bomber_id))
    for case in all_case:
        SummaryBomber.update(
            new_case_amount_sum=case.pending,
            new_case_cnt=case.cnt
        ).where(
            SummaryBomber.bomber_id == case.bomber_id,
            SummaryBomber.time == begin_date
        ).execute()
    return all_case


# ??????KP????????????
def get_kp_cleared(begin_date, end_date):
    auto_call_sql = """
        SELECT
            a.current_bomber_id, count( b.application_id ) 
        FROM
            (SELECT
                current_bomber_id, principal_part, late_fee_part,
                repay_at, application_id 
            FROM
                bomber.repayment_log 
            WHERE
                repay_at >= '%s' 
                AND repay_at < '%s' 
            GROUP BY 4, 5 ) a
        LEFT JOIN (
            SELECT
                cycle, bomber_id, promised_amount, promised_date,
                application_id, created_at 
            FROM
                bomber.auto_call_actions 
            WHERE
                created_at >= date_sub( '%s', INTERVAL 7 DAY ) 
                AND created_at < '%s' 
                AND promised_date IS NOT NULL 
                ) b ON a.current_bomber_id = b.bomber_id 
                 AND a.application_id = b.application_id 
                 AND date( a.repay_at ) <= date( b.promised_date ) 
                 AND date( a.repay_at ) >= date( b.created_at )
        LEFT JOIN bomber.bomber c ON a.current_bomber_id = c.id 
        WHERE
            b.promised_date >= '%s'
        GROUP BY 1
    """ % (begin_date, end_date, begin_date, end_date, begin_date)
    auto_call_results = run_all_sql(auto_call_sql)

    manual_sql = """
        SELECT
            a.current_bomber_id, count( b.application_id ) 
        FROM
            (SELECT
                current_bomber_id, principal_part, late_fee_part,
                repay_at, application_id, created_at 
            FROM
                bomber.repayment_log 
            WHERE
                repay_at >= '%s' 
                AND repay_at < '%s' 
                AND principal_part + late_fee_part > 0 
            GROUP BY 2, 5 ) a
        LEFT JOIN (
            SELECT
                cycle, bomber_id, promised_amount, promised_date, 
                application_id, created_at	
            FROM
                bomber.bombing_history 
            WHERE
                created_at >= date_sub( '%s', INTERVAL 7 DAY )  
                AND created_at < '%s' 
                AND promised_date IS NOT NULL 
                ) b ON a.current_bomber_id = b.bomber_id 
                AND a.application_id = b.application_id 
                AND date( a.repay_at ) <= date( b.promised_date ) 
                AND date( a.repay_at ) >= date( b.created_at )
        LEFT JOIN bomber.bomber c ON a.current_bomber_id = c.id 
        WHERE
            b.promised_date >= '%s'
        GROUP BY 1
    """ % (begin_date, end_date, begin_date, end_date, begin_date)
    manual_results = run_all_sql(manual_sql)

    sql_result = auto_call_results + manual_results
    result = {}
    for data in sql_result:
        if data[0] in result.keys():
            result[data[0]] += data[1]
            continue
        result[data[0]] = data[1]
    for key, value in result.items():
        (SummaryBomber
         .update(
            KP_cleared_cnt=value
         ).where(
            SummaryBomber.bomber_id == key,
            SummaryBomber.time == begin_date)
         ).execute()


# ??????????????????ptp??????(KP????????????)
def get_kp_today(begin_date, end_date):
    sql = """
        select bomber_id, count(distinct application_id)
        from( 
            SELECT bomber_id, application_id
            FROM bomber.auto_call_actions a
            WHERE promised_date >= '%s' AND created_at < '%s' 
                AND EXISTS(select 1 from bomber.application ba 
                           where a.application_id=ba.id 
                           and (ba.finished_at is null 
                                or ba.finished_at > '%s'))
            UNION 
            SELECT bomber_id, application_id
            FROM bomber.bombing_history b
            WHERE promised_date >= '%s' AND created_at < '%s'
                 AND EXISTS(select 1 from bomber.application ba 
                            where b.application_id=ba.id 
                            and (ba.finished_at is null 
                                 or ba.finished_at > '%s'))) result
        GROUP BY 1
    """ % (begin_date, end_date, begin_date, begin_date, end_date, begin_date)
    kp_today = run_all_sql(sql)

    for kp in kp_today:
        (SummaryBomber.update(
            KP_today_cnt=kp[1]
        ).where(
            SummaryBomber.time == begin_date,
            SummaryBomber.bomber_id == kp[0]
        )).execute()


# ??????ptp?????????????????????ptp????????????????????????????????????
def get_ptp_cnt(begin_date, end_date):
    today_due = []
    for sql_date in (begin_date, end_date):
        sql = """
            select bomber_id,count(distinct application_id) as cnt from 
            ( # ????????????????????????????????????P?????????
              select application_id,bomber_id,created_at 
              from bomber.auto_call_actions ba 
              where promised_date ='%s' # ??????????????????????????????P???
                and not exists ( select 1 from bomber.bombing_history bb 
                                 where bb.application_id = ba.application_id 
                                   and bb.bomber_id = ba.bomber_id 
                                   and bb.created_at>ba.created_at
                                   and bb.promised_date is not null  
                                   and bb.created_at < '%s')
              union #??????????????????????????????P?????????????????????????????????P???
              select b.application_id,b.bomber_id,a.cdt
              from bomber.bombing_history b
              inner join (
                select application_id,bomber_id,max(created_at) as cdt 
                from bomber.bombing_history bb
                where bb.created_at>date_sub('%s',interval 7 day)
                  and bb.created_at<'%s'
                  and promised_date is not null
                group by 1,2) a 
              on b.application_id=a.application_id 
              and b.bomber_id=a.bomber_id and a.cdt=b.created_at
              where b.promised_date ='%s'
              union #?????????????????????P
              select b.application_id,b.bomber_id,b.created_at
              from bomber.bombing_history b
              where b.promised_date ='%s'
                and b.created_at>'%s'
                and b.created_at<date_add('%s',interval 1 day)
            ) a
            where exists(select 1 from bomber.application ba 
                         where ba.id=a.application_id 
                           and ((ba.finished_at is null) 
                           or (ba.finished_at > '%s')))
            group by 1
        """ % (sql_date, begin_date, begin_date, begin_date, sql_date,
               sql_date, begin_date, begin_date, begin_date)
        datas = run_all_sql(sql)

        if sql_date == begin_date:
            today_due = datas
            for data in datas:
                (SummaryBomber.update(
                    ptp_today_cnt=data[1]
                ).where(
                    SummaryBomber.time == begin_date,
                    SummaryBomber.bomber_id == data[0]
                )).execute()
            continue
        nextday_due = datas
        for data in datas:
            (SummaryBomber.update(
                ptp_next_cnt=data[1]
            ).where(
                SummaryBomber.time == begin_date,
                SummaryBomber.bomber_id == data[0]
            )).execute()
    return [today_due, nextday_due]


# ??????ptp?????????????????????
def get_ptp_call_cnt(begin_date, end_date):
    today_followed = []
    for sql_data in (begin_date, end_date):
        sql = """
            select b.bomber_id,count(distinct b.application_id) as cnt 
            from (
              select a.* from 
              (
                select application_id,bomber_id,created_at 
                from bomber.auto_call_actions ba 
                where promised_date ='%s' # ??????????????????????????????P???
                  and not exists (select 1 from bomber.bombing_history bb 
                                  where bb.application_id = ba.application_id 
                                    and bb.bomber_id = ba.bomber_id 
                                    and bb.created_at>ba.created_at 
                                    and bb.promised_date is not null   
                                    and bb.created_at < '%s')
                union #??????????????????????????????P?????????????????????????????????P???
                select b.application_id,b.bomber_id,a.cdt
                from bomber.bombing_history b
                inner join (
                  select application_id,bomber_id,max(created_at) as cdt 
                  from bomber.bombing_history bb
                  where bb.created_at>date_sub('%s',interval 7 day)
                    and bb.created_at<'%s'
                    and promised_date is not null
                  group by 1,2) a 
                  on b.application_id=a.application_id 
                  and b.bomber_id=a.bomber_id and a.cdt=b.created_at
                where b.promised_date ='%s'
                union #?????????????????????P
                select b.application_id,b.bomber_id,b.created_at
                from bomber.bombing_history b
                where b.promised_date ='%s'
                  and b.created_at>'%s'
                  and b.created_at<date_add('%s',interval 1 day)
                ) a
              where exists(select 1 from bomber.application ba 
                           where ba.id=a.application_id 
                           and ((ba.finished_at is null) 
                           or (ba.finished_at > '%s')))
                and exists(select 1 from bomber.call_actions bc 
                           where a.application_id = bc.application_id 
                             and a.bomber_id = bc.bomber_id 
                             and bc.created_at>'%s' 
                             and bc.created_at< date_add('%s',interval 1 day) 
                             and bc.created_at>=a.created_at)
              union 
              select a.* from 
                (
                select application_id,bomber_id,created_at 
                from bomber.auto_call_actions ba 
                where promised_date ='%s' # ??????????????????????????????P???
                  and not exists ( select 1 from bomber.bombing_history bb 
                                   where bb.application_id = ba.application_id 
                                     and bb.bomber_id = ba.bomber_id 
                                     and bb.created_at>ba.created_at 
                                     and bb.promised_date is not null  
                                     and bb.created_at < '%s')
                union #??????????????????????????????P?????????????????????????????????P???
                select b.application_id,b.bomber_id,a.cdt
                from bomber.bombing_history b
                inner join (
                  select application_id,bomber_id,max(created_at) as cdt 
                  from bomber.bombing_history bb
                  where bb.created_at>date_sub('%s',interval 7 day)
                    and bb.created_at<'%s'
                    and promised_date is not null
                  group by 1,2) a 
                on b.application_id=a.application_id 
                and b.bomber_id=a.bomber_id and a.cdt=b.created_at
                where b.promised_date ='%s'
                union #?????????????????????P
                select b.application_id,b.bomber_id,b.created_at
                from bomber.bombing_history b
                where b.promised_date ='%s'
                  and b.created_at>'%s'
                  and b.created_at<date_add('%s',interval 1 day)
                ) a
              where exists(select 1 from bomber.application ba 
                           where ba.id=a.application_id 
                           and ba.finished_at > '%s' 
                           and ba.finished_at< date_add('%s',interval 1 day))
                ) b
            group by 1
        """ % (sql_data, begin_date, begin_date, begin_date, sql_data,
               sql_data, begin_date, begin_date, begin_date, begin_date,
               begin_date, sql_data, begin_date, begin_date, begin_date,
               sql_data, sql_data, begin_date, begin_date, begin_date,
               begin_date)
        datas = run_all_sql(sql)

        if sql_data == begin_date:
            today_followed = datas
            for data in datas:
                (SummaryBomber.update(
                    ptp_today_call_cnt=data[1]
                ).where(
                    SummaryBomber.bomber_id == data[0],
                    SummaryBomber.time == begin_date
                )).execute()
            continue
        nextday_followed = datas
        for data in datas:
            (SummaryBomber.update(
                ptp_next_call_cnt=data[1]
            ).where(
                SummaryBomber.bomber_id == data[0],
                SummaryBomber.time == begin_date
            )).execute()
    return [today_followed, nextday_followed]


# ????????????????????????(??????c2???c3???????????????????????????)
def get_new_case_cleared(begin_date, end_date):
    sql = """
        SELECT
            ptp_bomber AS bomber_id,
            sum( paid_amount ) AS pending 
        FROM
            (SELECT
            br.late_fee_part + br.principal_part AS paid_amount,
              br.ptp_bomber
           FROM	bomber.application ba
           INNER JOIN bomber.repayment_log br ON ba.id = br.application_id 
             AND date( ba.c1b_entry ) = date( br.repay_at ) 
             AND br.ptp_bomber is not null
           WHERE ba.c1b_entry > '%s' 
               AND ba.c1b_entry < '%s' 
           ) a 
        GROUP BY 1 
        UNION
        SELECT
            ptp_bomber AS bomber_id,
            sum( paid_amount ) AS pending 
        FROM
            (SELECT
            br.late_fee_part + br.principal_part AS paid_amount,
              br.ptp_bomber
           FROM	bomber.application ba
           INNER JOIN bomber.repayment_log br ON ba.id = br.application_id 
             AND date( ba.c2_entry ) = date( br.repay_at ) 
             AND br.ptp_bomber is not null
           WHERE ba.c2_entry > '%s' 
               AND ba.c2_entry < '%s' 
           ) a 
        GROUP BY 1 
        UNION
        SELECT
            ptp_bomber AS bomber_id,
            sum( paid_amount ) AS pending 
        FROM
            (SELECT
               br.late_fee_part + br.principal_part AS paid_amount,
               br.ptp_bomber
           FROM
                bomber.application ba
             INNER JOIN bomber.repayment_log br ON ba.id = br.application_id 
               AND date( ba.c3_entry ) = date( br.repay_at ) 
               AND br.ptp_bomber is not null
            WHERE ba.c3_entry > '%s' 
            AND ba.c3_entry < '%s' 
            ) a
        GROUP BY 1
    """ % (begin_date, end_date, begin_date, end_date,begin_date, end_date)
    case_cleared_sums = run_all_sql(sql)

    for clear in case_cleared_sums:
        (SummaryBomber.update(
            new_case_cleared_sum=clear[1]
        ).where(
            SummaryBomber.time == begin_date,
            SummaryBomber.bomber_id == clear[0]
        )).execute()


# ????????????????????????
@time_logger
def get_new_case_call(begin_date, end_date, real_query_time=False):
    sql = """
        SELECT
            bd.bomber_id,
            count( DISTINCT bd.application_id )
        FROM
            bomber.dispatch_app_history bd
            INNER JOIN bomber.call_actions bc 
               ON bd.application_id = bc.application_id 
            AND bd.bomber_id = bc.bomber_id 
            AND date( bd.entry_at ) = date( bc.created_at ) 
        WHERE
            entry_at > '%s' 
            AND entry_at < '%s' 
            AND partner_id IS NULL 
        GROUP BY 1
    """ % (begin_date, end_date)
    new_case_calls = run_all_sql(sql)

    if real_query_time:
        return new_case_calls

    for call in new_case_calls:
        (SummaryBomber.update(
            new_case_call_cnt=call[1]
        ).where(
            SummaryBomber.bomber_id == call[0],
            SummaryBomber.time == begin_date
        )).execute()
    return new_case_calls


# ??????????????????????????????
@time_logger
def get_calltime_avg(begin_date, end_date, real_query_time=False):
    autos_sql = """
        SELECT
            bb.id AS bomber_id,
            sum( talkduraction ) AS auto_talkduraction,
            count( 1 ) AS auto_jt_cnt 
        FROM
            auto_call.newcdr an
            LEFT JOIN bomber.bomber bb ON an.username = bb.username 
        WHERE
            an.timestart >= '%s' 
            AND an.timestart < '%s' 
            AND an.username != ' ' 
            AND an.STATUS = 'ANSWERED' 
            AND bb.id IS NOT NULL 
        GROUP BY 1
    """ % (begin_date, end_date)
    autos = run_all_sql(autos_sql)

    manual_sql = """
        SELECT
            bb.id AS bomber_id,
            sum( talkduraction ) AS manual_talkduraction,
            count( 1 ) AS manual_jt_cnt 
        FROM
            auto_call.newcdr an
            LEFT JOIN bomber.bomber bb ON an.callfrom = bb.ext 
        WHERE
            an.timestart >= '%s' 
            AND an.timestart < '%s' 
            AND ( ( an.callfrom LIKE '%s' ) OR ( an.callfrom LIKE '%s' ) ) 
            AND an.STATUS = 'ANSWERED' 
            AND bb.id IS NOT NULL 
            AND an.recording is not null
        GROUP BY 1
    """ % (begin_date, end_date, '5%', '3%')
    manuals = run_all_sql(manual_sql)

    datas = autos + manuals
    result = {}
    for data in datas:
        if data[0] in result.keys():
            result[data[0]][0] += data[1]
            result[data[0]][1] += data[2]
            continue
        result[data[0]] = [data[1], data[2]]

    if real_query_time:
        return result

    for key, value in result.items():
        (SummaryBomber.update(
            calltime_case_sum=value[0],
            calltime_case_cnt=value[1],
            calltime_case_avg=value[0] / value[1] if value[1] else 0
        ).where(
            SummaryBomber.time == begin_date,
            SummaryBomber.bomber_id == key
        )).execute()
    return result


# ??????????????????????????????
def get_no_calltime_avg(begin_date, end_date):
    manual_sql = """
        SELECT
            bb.id AS bomber_id,
            sum( talkduraction ) AS manual_talkduraction,
            count( 1 ) AS manual_jt_cnt 
        FROM
            auto_call.newcdr an
            LEFT JOIN bomber.bomber bb ON an.callfrom = bb.ext 
        WHERE
            an.timestart >= '%s' 
            AND an.timestart < '%s' 
            AND ( ( an.callfrom LIKE '%s' ) OR ( an.callfrom LIKE '%s' ) ) 
            AND (an.status!='ANSWERED' or an.recording is null) 
            AND bb.id IS NOT NULL 
        GROUP BY 1
    """ % (begin_date, end_date, '5%', '3%')
    manuals = run_all_sql(manual_sql)

    for data in manuals:
        (SummaryBomber.update(
            calltime_no_case_sum=data[1],
            calltime_no_case_cnt=data[2],
            calltime_no_case_avg=data[1] / data[2] if data[2] else 0
        ).where(
            SummaryBomber.time == begin_date,
            SummaryBomber.bomber_id == data[0]
        )).execute()


# ?????????????????????
@time_logger
def get_calltime_sum(begin_date, end_date, real_query_time=False):
    autos_sql = """
        SELECT
            bb.id AS bomber_id,
            sum( talkduraction ) AS auto_talkduraction
        FROM
            auto_call.newcdr an
            LEFT JOIN bomber.bomber bb ON an.username = bb.username 
        WHERE
            an.timestart >= '%s' 
            AND an.timestart < '%s' 
            AND an.username != ' '
            AND bb.id IS NOT NULL 
        GROUP BY 1
    """ % (begin_date, end_date)
    autos = run_all_sql(autos_sql)

    manual_sql = """
        SELECT
            bb.id AS bomber_id,
            sum( talkduraction ) AS manual_talkduraction
        FROM
            auto_call.newcdr an
            LEFT JOIN bomber.bomber bb ON an.callfrom = bb.ext 
        WHERE
            an.timestart >= '%s' 
            AND an.timestart < '%s' 
            AND ( ( an.callfrom LIKE '%s' ) OR ( an.callfrom LIKE '%s' ) ) 
            AND bb.id IS NOT NULL 
        GROUP BY 1
    """ % (begin_date, end_date, '5%', '3%')
    manuals = run_all_sql(manual_sql)

    datas = autos + manuals
    result = {}
    for data in datas:
        if data[0] in result.keys():
            result[data[0]] += data[1]
            continue
        result[data[0]] = data[1]
    if real_query_time:
        return result
    for key, value in result.items():
        (SummaryBomber.update(
            calltime_sum=value
        ).where(
            SummaryBomber.time == begin_date,
            SummaryBomber.bomber_id == key
        )).execute()
    return result


# ?????????????????????
def get_unfollowed(begin_date):
    sql = """
        SELECT
            bomber_id,
            count(1)
        FROM
            (
                SELECT
                    bd.application_id,
                    date(bd.entry_at) AS entry_at,
                    bd.bomber_id,
                    date(bd.out_at) AS out_at
                FROM
                    bomber.dispatch_app_history bd
                WHERE
                    (
                        out_at > date_add('%(begin_date)s', INTERVAL 1 DAY)
                        OR out_at IS NULL
                    )
                AND entry_at < date_add('%(begin_date)s', INTERVAL 1 DAY)
                AND entry_at > date_sub('%(begin_date)s', INTERVAL 30 DAY)
                AND partner_id IS NULL
                AND NOT EXISTS (
                    SELECT
                        1
                    FROM
                        bomber.call_actions bc
                    WHERE
                        bd.bomber_id = bc.bomber_id
                    AND bc.application_id = bd.application_id
                    AND bc.created_at < '%(begin_date)s'
                )
            ) a
        GROUP BY
            1
    """ % {'begin_date': begin_date}
    data = run_all_sql(sql)

    result = defaultdict(int)
    for d in data:
        result[d[0]] += d[1]

    bomber_list = []
    for key, value in result.items():
        bomber_list.append(key)
        (SummaryBomber.update(
            unfollowed_cnt=SummaryBomber.new_case_cnt + value
        ).where(
            SummaryBomber.time == begin_date,
            SummaryBomber.bomber_id == key
        )).execute()

    # ??????bomber_id?????????new_case_cnt??????
    (SummaryBomber.update(
        unfollowed_cnt=SummaryBomber.new_case_cnt
    ).where(
        SummaryBomber.time == begin_date,
        SummaryBomber.bomber_id.not_in(bomber_list)
    )).execute()


# ?????????????????????????????????
def get_unfollowed_call(begin_date):
    sql = """
        SELECT
            bomber_id,
            count(1)
        FROM
            (
                SELECT
                    bd.application_id,
                    date(bd.entry_at) AS entry_at,
                    bd.bomber_id,
                    date(bd.out_at) AS out_at
                FROM
                    bomber.dispatch_app_history bd
                WHERE
                    (
                        out_at > date_add('%(begin_date)s', INTERVAL 1 DAY)
                        OR out_at IS NULL
                    )
                AND entry_at < date_add('%(begin_date)s', INTERVAL 1 DAY)
                AND entry_at > date_sub('%(begin_date)s', INTERVAL 30 DAY)
                AND partner_id IS NULL
                AND NOT EXISTS (
                    SELECT
                        1
                    FROM
                        bomber.call_actions bc
                    WHERE
                        bd.bomber_id = bc.bomber_id
                    AND bc.application_id = bd.application_id
                    AND bc.created_at < '%(begin_date)s'
                )
            ) a
        WHERE
            EXISTS (
                SELECT
                    1
                FROM
                    bomber.call_actions bc
                WHERE
                    a.application_id = bc.application_id
                AND a.bomber_id = bc.bomber_id
                AND bc.created_at > '%(begin_date)s'
                AND bc.created_at < date_add('%(begin_date)s', INTERVAL 1 DAY)
                AND bc.created_at >= a.entry_at
            )
        OR EXISTS (
            SELECT
                1
            FROM
                bomber.application ba
            WHERE
                ba.id = a.application_id
            AND ba.finished_at > '%(begin_date)s'
            AND ba.finished_at < date_add('%(begin_date)s', INTERVAL 1 DAY)
        )
        GROUP BY
            1
    """ % {'begin_date': begin_date}
    data = run_all_sql(sql)

    result = defaultdict(int)
    for d in data:
        result[d[0]] += d[1]

    bomber_list = []
    for key, value in result.items():
        bomber_list.append(key)
        (SummaryBomber.update(
            unfollowed_call_cnt=SummaryBomber.new_case_call_cnt + value
        ).where(
            SummaryBomber.time == begin_date,
            SummaryBomber.bomber_id == key
        )).execute()

    # ??????bomber_id?????????new_case_cnt??????
    update_sql = (SummaryBomber
                  .update(unfollowed_call_cnt=SummaryBomber.new_case_call_cnt)
                  .where(SummaryBomber.time == begin_date))
    if bomber_list:
        update_sql = update_sql.where(SummaryBomber.bomber_id
                                      .not_in(bomber_list))
    update_sql.execute()
    return result


# summary ???????????????????????????summary_bomber????????????????????????
@action(MessageAction.UPDATE_SUMMARY_NEW)
def update_summary_new(payload, msg_id):
    end_date = date.today()
    begin_date = end_date - timedelta(days=1)

    worker_log = (WorkerLog.select(fn.COUNT(WorkerLog.action).alias('logs'))
                  .where(WorkerLog.created_at >= end_date,
                         WorkerLog.action == 'UPDATE_SUMMARY_NEW')
                  .first())
    if worker_log and worker_log.logs >= 5:
        return

    get_new_case_amount(begin_date, end_date)
    get_kp_cleared(begin_date, end_date)
    get_kp_today(begin_date, end_date)
    get_ptp_cnt(begin_date, end_date)
    get_ptp_call_cnt(begin_date, end_date)
    get_new_case_cleared(begin_date, end_date)
    get_new_case_call(begin_date, end_date)
    get_calltime_avg(begin_date, end_date)
    get_no_calltime_avg(begin_date, end_date)
    get_calltime_sum(begin_date, end_date)


# -------------------------------- ??????cycle???????????? --------------------------
def get_cycle_claimed(begin_date, end_date):
    sql = """
        select cycle,count(1)
        from bomber.application where cycle in (1,2,3,4)
        and (finished_at is null or (finished_at>'%s'))
        and created_at>'2018-09-01'
        group by 1
    """ % begin_date
    result = run_all_sql(sql)
    return result


# ??????cycle???????????????????????????
@time_logger
def cycle_new_case(begin_date, end_date, real_time_query=False):
    sql = """
        SELECT
            1 AS cycle,
            count( ba.id ),
            sum( bo.principal_pending + late_fee_pending + 
                interest_pending ) AS pending 
        FROM
            bomber.application ba
            INNER JOIN bill_java.overdue bo ON ba.id = bo.application_id 
            AND date( ba.created_at ) = bo.which_day_overdue 
        WHERE
            ba.created_at > '%s' 
            AND ba.created_at < '%s' 
        UNION
        SELECT
            2 AS cycle,
            count( 1 ),
            sum( bo.principal_pending + late_fee_pending + 
                interest_pending ) AS pending 
        FROM
            bomber.application ba
            INNER JOIN bill_java.overdue bo ON ba.id = bo.application_id 
            AND date( ba.c1b_entry ) = bo.which_day_overdue
        WHERE
            c1b_entry > '%s' 
            AND c1b_entry < '%s' 
        UNION
        SELECT
            3 AS cycle,
            count( 1 ),
            sum( bo.principal_pending + late_fee_pending + 
                 interest_pending ) AS pending 
        FROM
            bomber.application ba
            INNER JOIN bill_java.overdue bo ON ba.id = bo.application_id 
            AND date( ba.c2_entry ) = bo.which_day_overdue 
        WHERE
            c2_entry > '%s' 
            AND c2_entry < '%s' 
        UNION
        SELECT
            4 AS cycle,
            count( 1 ),
            sum( bo.principal_pending + late_fee_pending + 
                 interest_pending ) AS pending 
        FROM
            bomber.application ba
            INNER JOIN bill_java.overdue bo ON ba.id = bo.application_id 
            AND date( ba.c3_entry ) = bo.which_day_overdue
        WHERE
            c3_entry > '%s' 
            AND c3_entry < '%s'
    """ % (begin_date, end_date, begin_date, end_date,
           begin_date, end_date, begin_date, end_date)
    all_datas = run_all_sql(sql)

    if real_time_query:
        return all_datas

    for data in all_datas:
        (SummaryBomber.update(
            new_case_amount_sum=data[2],
            new_case_cnt=data[1]
        ).where(
            SummaryBomber.time == begin_date,
            SummaryBomber.bomber_id == data[0],
            SummaryBomber.cycle == data[0]
        )).execute()
    return all_datas


# ????????????????????????
@time_logger
def get_cycle_new_case_call(begin_date, end_date, real_time_query=False):
    sql = """
        SELECT
            1 AS cycle,
            count( DISTINCT ba.id ) 
        FROM
            bomber.application ba
            INNER JOIN bomber.call_actions bc ON ba.id = bc.application_id 
            AND date( ba.created_at ) = date( bc.created_at ) 
        WHERE
            ba.created_at > '%s' 
            AND ba.created_at < '%s' 
        UNION
        SELECT
            2 AS cycle,
            count( DISTINCT ba.id ) 
        FROM
            bomber.application ba
            INNER JOIN bomber.call_actions bc ON ba.id = bc.application_id 
            AND date( ba.c1b_entry ) = date( bc.created_at ) 
        WHERE
            ba.c1b_entry > '%s' 
            AND ba.c1b_entry < '%s'
        UNION
        SELECT
            3 AS cycle,
            count( DISTINCT ba.id ) 
        FROM
            bomber.application ba
            INNER JOIN bomber.call_actions bc ON ba.id = bc.application_id 
            AND date( ba.c2_entry ) = date( bc.created_at ) 
        WHERE
            ba.c2_entry > '%s' 
            AND ba.c2_entry < '%s'
        UNION
        SELECT
            4 AS cycle,
            count( DISTINCT ba.id ) 
        FROM
            bomber.application ba
            INNER JOIN bomber.call_actions bc ON ba.id = bc.application_id 
            AND date( ba.c3_entry ) = date( bc.created_at ) 
        WHERE
            ba.c3_entry > '%s' 
            AND ba.c3_entry < '%s'
    """ % (begin_date, end_date, begin_date, end_date,
           begin_date, end_date, begin_date, end_date)
    cycle_datas = run_all_sql(sql)

    if real_time_query:
        return cycle_datas

    for data in cycle_datas:
        (SummaryBomber.update(
            new_case_call_cnt=data[1]
        ).where(
            SummaryBomber.time == begin_date,
            SummaryBomber.cycle == data[0],
            SummaryBomber.bomber_id == data[0]
        )).execute()
    return cycle_datas


def get_cycle_new_case_cleared(begin_date, end_date):
    sql = """
        SELECT
            '1' AS cycle, count( DISTINCT id ), 
            sum( paid_amount ) AS pending 
        FROM
            (SELECT ba.id, br.repay_at, 
             br.late_fee_part + br.principal_part AS paid_amount 
             FROM
                bomber.application ba
                INNER JOIN bomber.repayment_log br ON ba.id = br.application_id 
                AND date( ba.created_at ) = date( br.repay_at ) 
             WHERE ba.created_at > '%s' 
                AND ba.created_at < '%s' 
        GROUP BY 1, 2 ) a 
        UNION
        SELECT 
            '2' AS cycle, count( DISTINCT id ), 
            sum( paid_amount ) AS pending 
        FROM
            (SELECT ba.id, br.repay_at, 
            br.late_fee_part + br.principal_part AS paid_amount 
           FROM
                bomber.application ba
                INNER JOIN bomber.repayment_log br ON ba.id = br.application_id 
                AND date( ba.c1b_entry ) = date( br.repay_at ) 
             WHERE ba.c1b_entry > '%s' 
                 AND ba.c1b_entry < '%s' 
        GROUP BY 1, 2) a
    """ % (begin_date, end_date, begin_date, end_date)
    cycle_cleared = run_all_sql(sql)

    for i in cycle_cleared:
        (SummaryBomber.update(
            new_case_cleared_sum=i[2]
        ).where(
            SummaryBomber.cycle == i[0],
            SummaryBomber.bomber_id == i[0],
            SummaryBomber.time == begin_date
        )).execute()


def get_cycle_case_made_cnt(begin_date, end_date):
    sql = """
        select cycle,count(distinct application) from (
            select distinct cycle,application from bomber.auto_call_list_record
            where created_at >= '%s'
            and created_at < '%s'
            and called_counts <> 0
            and cycle in (1,2,3,4)
            union
            select distinct cycle,application_id from bomber.call_actions
            where created_at >= '%s'
            and created_at < '%s'
            and cycle in (1,2,3,4)
            ) c
        group by 1
    """ % (begin_date, end_date, begin_date, end_date)
    case_made_datas = run_all_sql(sql)

    for case_made_data in case_made_datas:
        (SummaryBomber.update(
            case_made_cnt=case_made_data[1]
        ).where(
            SummaryBomber.time == begin_date,
            SummaryBomber.cycle == case_made_data[0],
            SummaryBomber.bomber_id == case_made_data[0]
        )).execute()


# ??????cycle???????????????
@action(MessageAction.SUMMARY_NEW_CYCLE)
def summary_new_cycle(payload, msg_id):
    end_date = date.today()
    begin_date = end_date - timedelta(days=1)

    worker_log = (WorkerLog.select(fn.COUNT(WorkerLog.action).alias('logs'))
                  .where(WorkerLog.created_at >= end_date,
                         WorkerLog.action == 'SUMMARY_NEW_CYCLE')
                  .first())
    if worker_log and worker_log.logs >= 5:
        return

    cycle_datas = (SummaryBomber
                   .select(fn.SUM(SummaryBomber.new_case_amount_sum)
                           .alias('new_case_amount_sum'),
                           fn.SUM(SummaryBomber.new_case_cleared_sum)
                           .alias('new_case_cleared_sum'),
                           fn.SUM(SummaryBomber.case_made_cnt)
                           .alias('case_made_cnt'),
                           fn.SUM(SummaryBomber.case_connect_cnt)
                           .alias('case_connect_cnt'),
                           fn.SUM(SummaryBomber.promised_cnt)
                           .alias('promised_cnt'),
                           fn.SUM(SummaryBomber.promised_amount)
                           .alias('promised_amount'),
                           fn.SUM(SummaryBomber.cleared_cnt)
                           .alias('cleared_cnt'),
                           fn.SUM(SummaryBomber.cleared_amount)
                           .alias('cleared_amount'),
                           fn.SUM(SummaryBomber.new_case_cnt)
                           .alias('new_case_cnt'),
                           fn.SUM(SummaryBomber.new_case_call_cnt)
                           .alias('new_case_call_cnt'),
                           fn.SUM(SummaryBomber.unfollowed_cnt)
                           .alias('unfollowed_cnt'),
                           fn.SUM(SummaryBomber.unfollowed_call_cnt)
                           .alias('unfollowed_call_cnt'),
                           fn.SUM(SummaryBomber.call_cnt).alias('call_cnt'),
                           fn.SUM(SummaryBomber.sms_cnt).alias('sms_cnt'),
                           fn.SUM(SummaryBomber.call_connect_cnt)
                           .alias('call_connect_cnt'),
                           fn.SUM(SummaryBomber.ptp_today_cnt)
                           .alias('ptp_today_cnt'),
                           fn.SUM(SummaryBomber.ptp_today_call_cnt)
                           .alias('ptp_today_call_cnt'),
                           fn.SUM(SummaryBomber.ptp_next_cnt)
                           .alias('ptp_next_cnt'),
                           fn.SUM(SummaryBomber.ptp_next_call_cnt)
                           .alias('ptp_next_call_cnt'),
                           fn.SUM(SummaryBomber.KP_cleared_cnt)
                           .alias('KP_cleared_cnt'),
                           fn.SUM(SummaryBomber.KP_today_cnt)
                           .alias('KP_today_cnt'),
                           fn.SUM(SummaryBomber.work_ind).alias('work_ind'),
                           fn.SUM(SummaryBomber.calltime_sum)
                           .alias('calltime_sum'),
                           fn.SUM(SummaryBomber.calltime_case_sum)
                           .alias('calltime_case_sum'),
                           fn.SUM(SummaryBomber.calltime_case_cnt)
                           .alias('calltime_case_cnt'),
                           fn.SUM(SummaryBomber.calltime_no_case_sum)
                           .alias('calltime_no_case_sum'),
                           fn.SUM(SummaryBomber.calltime_no_case_cnt)
                           .alias('calltime_no_case_cnt'),
                           SummaryBomber.cycle.alias('cycle'))
                   .where(SummaryBomber.time == begin_date,
                          SummaryBomber.cycle << Cycle.values())
                   .group_by(SummaryBomber.cycle))

    for cycle_data in cycle_datas:
        SummaryBomber.create(
            bomber_id=cycle_data.cycle,
            time=begin_date,
            cycle=cycle_data.cycle,
            new_case_amount_sum=cycle_data.new_case_amount_sum,  # ????????????(??????)
            new_case_cleared_sum=cycle_data.new_case_cleared_sum,  # ????????????????????????
            new_case_cleard_rate=0,
            case_made_cnt=cycle_data.case_made_cnt,  # ????????????
            case_made_rate=0,
            case_connect_cnt=cycle_data.case_connect_cnt,  # ????????????
            case_connect_rate=0,
            promised_cnt=cycle_data.promised_cnt,  # ptp??????
            promised_amount=cycle_data.promised_amount,  # ptp??????
            cleared_cnt=cycle_data.cleared_cnt,  # ????????????
            cleared_amount=cycle_data.cleared_amount,  # ????????????
            new_case_cnt=cycle_data.new_case_cnt,  # ???????????????1???2?????????
            new_case_call_cnt=cycle_data.new_case_call_cnt,  # ???????????????????????????
            unfollowed_cnt=cycle_data.unfollowed_cnt,
            unfollowed_call_cnt=cycle_data.unfollowed_call_cnt,
            call_cnt=cycle_data.call_cnt,  # ???????????????
            sms_cnt=cycle_data.sms_cnt,  # ???????????????
            call_connect_cnt=cycle_data.call_connect_cnt,  # ???????????????
            calltime_case_avg=0,  # ????????????????????????  (????????????)
            ptp_today_cnt=cycle_data.ptp_today_cnt,  # ??????ptp??????
            ptp_today_call_cnt=cycle_data.ptp_today_call_cnt,  # ??????ptp??????????????????
            ptp_next_cnt=cycle_data.ptp_next_cnt,  # ??????ptp?????????
            ptp_next_call_cnt=cycle_data.ptp_next_call_cnt,   # ?????????????????????
            KP_cleared_cnt=cycle_data.KP_cleared_cnt,  # kp?????????
            KP_today_cnt=cycle_data.KP_today_cnt,  # ????????????ptp??????
            KP_cleared_rate=0,
            work_ind=cycle_data.work_ind,  # ??????????????????
            calltime_sum=cycle_data.calltime_sum,  # ???????????????
            calltime_case_sum=cycle_data.calltime_case_sum,
            calltime_case_cnt=cycle_data.calltime_case_cnt,
            calltime_no_case_sum=cycle_data.calltime_no_case_sum,
            calltime_no_case_cnt=cycle_data.calltime_no_case_cnt,
            work_time_sum=cycle_data.work_time_sum  # ????????????
        )

    cycle_claimed = get_cycle_claimed(begin_date, end_date)
    for claimed in cycle_claimed:
        (SummaryBomber.update(
            claimed_cnt=claimed[1]
        ).where(
            SummaryBomber.time == begin_date,
            SummaryBomber.cycle == claimed[0],
            SummaryBomber.bomber_id == claimed[0]
        )).execute()

    # ???????????????????????????
    cycle_new_case(begin_date, end_date)

    # ????????????????????????
    get_cycle_new_case_call(begin_date, end_date)

    # ????????????????????????
    get_cycle_new_case_cleared(begin_date, end_date)

    # ??????cycle???????????????????????????????????????????????????????????????
    get_cycle_case_made_cnt(begin_date, end_date)

    # ?????????????????????(?????????)
    all_datas = (SummaryBomber.filter(SummaryBomber.time == begin_date))
    for data in all_datas:
        cl_rat = (data.new_case_cleared_sum / data.new_case_amount_sum
                  if data.new_case_amount_sum else 0) * 100
        data.new_case_cleard_rate = cl_rat

        case_made_rate = (data.case_made_cnt / data.claimed_cnt
                          if data.claimed_cnt else 0) * 100
        data.case_made_rate = case_made_rate

        case_connect_rate = (data.case_connect_cnt / data.case_made_cnt
                             if data.case_made_cnt else 0) * 100
        data.case_connect_rate = case_connect_rate

        calltime_case_avg = (data.calltime_case_sum / data.calltime_case_cnt
                             if data.calltime_case_cnt else 0)
        data.calltime_case_avg = calltime_case_avg

        calltime_no_case_avg = (data.calltime_no_case_sum /
                                data.calltime_no_case_cnt
                                if data.calltime_no_case_cnt else 0)
        data.calltime_no_case_avg = calltime_no_case_avg

        KP_cleared_rate = (data.KP_cleared_cnt / data.KP_today_cnt
                           if data.KP_today_cnt else 0) * 100
        data.KP_cleared_rate = KP_cleared_rate

        data.save()


@action(MessageAction.MODIFY_BILL)
def modify_bill(payload, msg_id):
    application_id = payload.get('external_id')
    principal_paid = Decimal(payload.get('principal_paid', 0))
    late_fee = Decimal(payload.get('late_fee', 0))
    late_fee_paid = Decimal(payload.get('late_fee_paid', 0))
    overdue_days = payload.get('overdue_days')
    sub_bill_id = payload.get('bill_sub_id')
    partner_bill_id = payload.get('partner_bill_id')
    if not application_id:
        logging.warning('payload has no external_id. {}'.format(str(payload)))
        return
    if not overdue_days:
        logging.info("application %s not overdue" % application_id)
        return

    item = (OldLoanApplication
            .get_or_none(OldLoanApplication.application_id ==
                         application_id))
    if item:
        start_old_application(item, cancel=True)

    overdue_bill = (OverdueBill.select()
                    .where(OverdueBill.external_id == application_id,
                           OverdueBill.sub_bill_id == sub_bill_id)
                    .first())
    application = (Application.filter(Application.id == application_id)
                   .first())
    if not overdue_bill:
        if not application:
            logging.info('application %s not in bomber, let it in bomber now',
                         application_id)
            send_to_default_q(MessageAction.APPLICATION_BOMBER, {
                'id': application_id,
                'bill_sub_id': sub_bill_id
            })
            return
    else:
        application = (Application
                       .filter(Application.id == overdue_bill.collection_id)
                       .first())

    with db.atomic():
        application.status = ApplicationStatus.UNCLAIMED.value
        application.finished_at = None
        application.paid_at = None
        application.save()
        if overdue_bill:
            overdue_bill.status = ApplicationStatus.UNCLAIMED.value
            overdue_bill.finished_at = None
            overdue_bill.save()
            repayment = (RepaymentLog.update(no_active = 1)
                         .where(RepaymentLog.application == application.id,
                                RepaymentLog.partner_bill_id == partner_bill_id,
                                RepaymentLog.overdue_bill_id == overdue_bill.id))
        else:
            repayment = (RepaymentLog.update(no_active=1)
                         .where(RepaymentLog.application == application.id,
                                RepaymentLog.partner_bill_id == partner_bill_id))
        repayment_num = repayment.execute()
        logging.info("modify_bill no active repayment count:%s" % repayment_num)

        if not application.latest_bomber_id:
            return

        bomber_id = application.latest_bomber_id
        (DispatchAppHistory.update(
            out_at=None,
            out_overdue_days=overdue_days,
            out_principal_pending=(application.amount - principal_paid),
            out_late_fee_pending=(late_fee - late_fee_paid)
        ).where(
            DispatchAppHistory.application == application.id,
            DispatchAppHistory.bomber_id == bomber_id)).execute()


# ???????????????ids
def get_change_bomber():
    cycle_role_map = {5: Cycle.C1B.value, 6: Cycle.C2.value, 8: Cycle.C3.value}
    result = {}
    bomber_logs = (BomberLog.select(BomberLog.bomber_id,
                                    BomberLog.role_id,
                                    BomberLog.operation,
                                    Bomber.group_id)
                   .join(Bomber, JOIN_INNER,
                         on=BomberLog.bomber_id == Bomber.id)
                   .where(fn.DATE(BomberLog.created_at) == date.today(),
                          BomberLog.role_id << list(cycle_role_map.keys()),#C1b,c2,c3
                          BomberLog.operation << (0, 1), #0?????????1?????????3??????
                          Bomber.instalment == 0) #?????????????????????
                   .dicts())
    for b_log in bomber_logs:
        cycle = cycle_role_map.get(b_log["role_id"])
        group_id = b_log["group_id"]
        if cycle in result:
            if group_id not in result[cycle]:
                result[cycle][group_id] = {
                    "cycle": cycle,
                    "del_ids": [],
                    "new_ids": []
                }
        else:
            result[cycle] = {group_id: {
                "cycle": cycle,
                "del_ids": [],
                "new_ids": []}
            }
        if b_log["operation"] == 0:
            result[cycle][group_id]["del_ids"].append(b_log["bomber_id"])
    # result ???????????????????????????
    if result:
        bombers = (Bomber.select()
                   .where(Bomber.role.in_(list(cycle_role_map.keys())),
                          Bomber.is_del == 0,
                          Bomber.instalment == 0))
        for b in bombers:
            cycle_result = result.get(cycle_role_map[b.role_id], {})
            role_result = cycle_result.get(b.group_id)
            if not role_result:
                continue
            role_result["new_ids"].append(b.id)
        resutl_list = []
        for cycle, group_dict in result.items():
            resutl_list.extend(list(group_dict.values()))
        return resutl_list
    return []


# ???????????????application
def get_total_application(cycle, del_ids, new_ids,
                          type=ApplicationType.CASH_LOAN.value):
    bomber_list = del_ids + new_ids
    all_apps = (Application.select(Application.id,
                                   Application.latest_bomber_id.alias(
                                       "latest_bomber_id"),
                                   Application.promised_date,
                                   Bomber.partner_id.alias("partner_id"))
                .join(Bomber, JOIN_LEFT_OUTER,
                      Application.latest_bomber == Bomber.id)
                .where(Application.cycle == cycle,
                       Application.status != ApplicationStatus.REPAID.value,
                       Application.latest_bomber_id << bomber_list,
                       Application.type == type)
                .order_by(Application.id)
                .dicts())
    return all_apps


# ?????????????????????????????????bomber?????????????????????
def get_average_number(app_nums, bomber_nums):
    average = app_nums // bomber_nums
    remainder = app_nums % bomber_nums
    average_list = [average for i in range(bomber_nums)]
    if remainder == 0:
        return average_list
    for i in range(remainder):
        average_list[i] += 1
    #  ??????????????????????????????????????????????????????????????????
    random.shuffle(average_list)
    return average_list


# ???appliciton??????????????????
def classified_statistic_apps(apps):
    result = {}
    # ???????????????bomber_id ???????????????????????????
    for app in apps:
        #     ????????????p?????????p????????????
        latest_bomber_id = app["latest_bomber_id"]
        if latest_bomber_id not in result:
            result[latest_bomber_id] = {
                "bid":latest_bomber_id,
                "p_list": [],
                "np_list": [],
                "partner_id": app["partner_id"] if app["partner_id"] else "",
            }
        promised_date = app.get("promised_date")
        if not promised_date or promised_date.date() < date.today():
            result[latest_bomber_id]['np_list'].append(app["id"])
        else:
            result[latest_bomber_id]['p_list'].append(app["id"])
    return result


#     ??????????????????,????????????????????????????????????
def get_surplus_application(new_ids, del_ids, average_nums, classified_apps):
    surplus_apps = []
    # ??????id??????????????????????????????id????????????????????????
    for del_id in del_ids:
            del_res = classified_apps.get(del_id,{})
            p_list = del_res.get("p_list", [])
            np_list = del_res.get("np_list", [])
            del_res["need_num"] = -(len(p_list) + len(np_list))
            del_res["to_list"] = np_list + p_list
            surplus_apps.extend(p_list)
            surplus_apps.extend(np_list)
    #  ????????????????????????p?????????p????????????????????????????????????????????????
    for index, bid in enumerate(new_ids):
        average = average_nums[index]
        bomber_app = classified_apps.get(bid)
        if not bomber_app:
            # ??????partner_id
            bomber = (Bomber.select(Bomber.partner_id)
                            .where(Bomber.id == bid)
                            .first())
            bomber_app = {
                "bid": bid,
                "p_list": [],
                "p_num": 0,
                "np_list": [],
                "np_num": 0,
                "need_num": average,
                "partner_id": bomber.partner_id if bomber else ''
            }
            classified_apps[bid] = bomber_app
        else:
            p_num = len(bomber_app["p_list"])
            np_num = len(bomber_app["np_list"])
            # ?????????p???????????????????????????????????????????????????????????????????????????
            if p_num > average:
                bomber_app["need_num"] = - np_num
            else:
                bomber_app["need_num"] = average - (p_num + np_num)
            bomber_app["p_num"] = p_num
            bomber_app["np_num"] = np_num
        # ???????????????????????????????????????
        if bomber_app["need_num"] < 0:
            # ??????????????????????????????????????????????????????
            random.shuffle(bomber_app["np_list"])
            res_over = bomber_app["np_list"][:-bomber_app["need_num"]]
            bomber_app["to_list"] = res_over
            surplus_apps.extend(res_over)
    # ??????need_num????????????
    classified_apps_list = sorted(classified_apps.values(),
                                  key=lambda x:x["need_num"],
                                  reverse=True)
    return surplus_apps, classified_apps_list


# ????????????????????????????????????
def update_applications(surplus_apps, classified_apps, cycle):
    # ????????????????????????
    random.shuffle(surplus_apps)
    for app in classified_apps:
        status = 0
        try:
            if app["need_num"] > 0:
                from_list = surplus_apps[:app["need_num"]]
                # ??????surplus_apps????????????
                for i in from_list: surplus_apps.remove(i)
                app["from_list"] = from_list
                with db.atomic():
                    q = Application.update(
                        {Application.latest_bomber_id: app["bid"]}).where(
                        Application.id.in_(from_list))
                    q.execute()
                    # ????????????
                    in_record_params = {
                        "dest_bomber_id": app["bid"],
                        "application_ids": from_list,
                        "dest_partner_id": app["partner_id"],
                        "cycle": cycle,
                    }
                    new_in_record(**in_record_params)
                    status = 1
            elif app["need_num"] < 0:
                #????????????
                out_record_params = {
                    "src_bomber_id": app["bid"],
                    "application_ids": app["to_list"]
                }
                new_out_record(**out_record_params)
                status = 1
            else:
                status = 1
        except Exception as e:
            logging.error("????????????,params:%s,error:%s"%(app,str(e)))
        #??????????????????
        log_params = {
            "bomber_id": app["bid"],
            "form_ids": json.dumps(app.get("from_list", [])),
            "to_ids": json.dumps(app.get("to_list", [])),
            "need_num": app.get("need_num"),
            "np_ids": json.dumps(app.get("np_list", [])),
            "p_ids": json.dumps(app.get("p_list", [])),
            "status": status
        }
        DispatchAppLogs.create(**log_params)
    return classified_apps


# ????????????????????????????????????
def get_instalment_change_bomber():
    result ={}
    bomber_logs = (BomberLog.select(BomberLog.bomber_id,
                                    BomberLog.operation,
                                    Bomber.instalment,
                                    Bomber.group_id)
                  .join(Bomber, JOIN_INNER,
                        on=BomberLog.bomber_id == Bomber.id)
                  .where(fn.DATE(BomberLog.created_at) == date.today(),
                         BomberLog.operation << [0,1],
                         Bomber.instalment > 0)
                  .dicts())
    for bl in bomber_logs:
        cycle = bl["instalment"]
        group_id = bl["group_id"]
        if cycle not in result:
            result[cycle] = {group_id: {
                                    "cycle": cycle,
                                    "del_ids": [],
                                    "new_ids": []
                                    }}
        else:
            if group_id not in result[cycle]:
                result[cycle][group_id] = {
                                            "cycle": cycle,
                                            "del_ids": [],
                                            "new_ids": []}
        if bl["operation"] == 0:
            result[cycle][group_id]["del_ids"].append(bl["bomber_id"])
    if result:
        instalments = list(result.keys())
        bombers = (Bomber.select()
                   .where(Bomber.instalment << instalments,
                          Bomber.is_del == 0))
        for b in bombers:
            cycle_result = result.get(b.instalment, {})
            group_result = cycle_result.get(b.group_id)
            if not group_result:
                continue
            group_result["new_ids"].append(b.id)
        result_list = []
        for cycle,group_dict in result.items():
            result_list.extend(list(group_dict.values()))
        return result_list
    return []

def instalment_update_applications(surplus_apps, classified_apps, cycle):
    end = 0
    for app in classified_apps:
        if app["need_num"] <= 0:
            continue
        start = end
        end = start + app["need_num"]
        aids = surplus_apps[start:end]
        app["from_list"] = aids
        status = 0
        with db.atomic():
            q = (Application.update(last_bomber = Application.latest_bomber,
                                    latest_bomber = app["bid"],
                                    ptp_bomber = None)
                 .where(Application.id << aids)
                 .execute())
            # ???????????????
            record_param = {
                "cycle": cycle,
                "application_ids": aids,
                "dest_bomber_id": app["bid"],
                "dest_partner_id": app["partner_id"],
            }
            out_and_in_record_instalment(**record_param)
            status = 1
        # ??????????????????
        log_params = {
            "bomber_id": app["bid"],
            "form_ids": json.dumps(app.get("from_list", [])),
            "to_ids": json.dumps(app.get("to_list", [])),
            "need_num": app.get("need_num"),
            "np_ids": json.dumps(app.get("np_list", [])),
            "p_ids": json.dumps(app.get("p_list", [])),
            "status": status
        }
        DispatchAppLogs.create(**log_params)
    return classified_apps

# ????????????????????????
def change_bomber_dispatch_apps(change_bombers,
                                type=ApplicationType.CASH_LOAN.value):
    if not change_bombers:
        return
    for bombers in change_bombers:
        del_ids = bombers.get("del_ids", [])
        new_ids = bombers.get("new_ids", [])
        cycle = bombers.get("cycle")
        if not all([new_ids, cycle]):
            logging.info(
                "?????????????????????????????????,bomber:%s,type:%s" % (bombers, type))
            continue
        # ?????????apps
        apps = get_total_application(cycle, del_ids, new_ids, type)
        if not apps:
            logging.info(
                "?????????????????????????????????,bomber:%s,type:%s" % (bombers, type))
            continue
        # ?????????????????????
        average_nums = get_average_number(len(apps), len(new_ids))
        # ????????????apps
        classified_apps = classified_statistic_apps(apps)
        # ?????????????????????????????????????????????
        superlus_apps, classified_apps = get_surplus_application(new_ids,
                                                                 del_ids,
                                                                 average_nums,
                                                                 classified_apps)
        # ????????????????????????
        if type == ApplicationType.CASH_LOAN.value:
            result = update_applications(superlus_apps, classified_apps, cycle)
        elif type == ApplicationType.CASH_LOAN_STAGING.value:
            result = instalment_update_applications(superlus_apps,
                                                    classified_apps,
                                                    cycle)
        else:
            logging.info("????????????????????????,unknown type:%s" % type)

        logging.info("???????????????????????????:result:%s,type:%s" % (result, type))


#bomber???????????????????????????
@action(MessageAction.BOMBER_CHANGE_DISPATCH_APPS)
def bomber_dispatch_applications(payload, msg_id):
    #????????????????????????????????????????????????????????????bomber_log?????????
    change_bombers = get_change_bomber()
    instalment_change_bombers = get_instalment_change_bomber()
    params = {ApplicationType.CASH_LOAN.value: change_bombers,
              ApplicationType.CASH_LOAN_STAGING.value: instalment_change_bombers}
    for type,bombers in params.items():
        change_bomber_dispatch_apps(change_bombers=bombers,type=type)


@action(MessageAction.REPAIR_BOMBER)
def repair_bomber(payload, msg_id):
    app_mobile = payload['app_mobile']
    username = payload.get('user_name')
    logging.info('start repair bomber, number: %s' % app_mobile)

    # ?????????????????????EC????????????EC??????????????????????????????????????????
    if 'mobile_no' in payload and payload['mobile_no']:
        mobile = number_strip(str(payload['mobile_no']))[:64]
        name = payload.get('mobile_name')
        application = Application.filter(Application.user_mobile_no == mobile)
        if application.exists():
            repair_contact(app_mobile, application, username)
        add_relationship(app_mobile, mobile, username, name)

    if 'tel_no' in payload and payload['tel_no']:
        tel_no = number_strip(str(payload['tel_no']))[:64]
        name = payload.get('tel_name')
        application = Application.filter(Application.user_mobile_no == tel_no)
        if application.exists():
            repair_contact(app_mobile, application, username)
        add_relationship(app_mobile, tel_no, username, name)


def repair_contact(number, application, name):
    # ?????????ec??????????????????????????????contact???
    application = application.first()
    contact = (Contact
               .filter(Contact.user_id == application.user_id,
                       Contact.number == number))
    if not contact.exists():
        Contact.create(
            user_id=application.user_id,
            name=name,
            number=number,
            relationship=Relationship.FAMILY.value,
            source='repair ec',
            real_relationship=Relationship.FAMILY.value
        )
    logging.info('add repair contact success, number: %s' % number)


def add_relationship(number, ec_number, username, name):
    # ????????????
    query = (TotalContact
             .objects(src_number=str(number),
                      dest_number=ec_number,
                      source=20,
                      is_calc=False
                      )
             .first())
    if not query:
        TotalContact(
            src_number=str(number),
            src_name=username,
            dest_number=ec_number,
            dest_name=name,
            source=20).save()
    logging.info('add relationship success, number: %s' % number)


# ??????????????????????????????
def get_summary_daily_time():
    mid_time_t1 = datetime.strptime('12:40:00', '%H:%M:%S')
    mid_time_t2 = datetime.strptime('17:20:00', '%H:%M:%S')
    now_date = datetime.now()
    now_date_time = now_date.time()
    today_str = str(now_date.date())
    if now_date_time < mid_time_t1.time():
        yes_date = now_date - timedelta(days=1)
        yes_date_str = str(yes_date.date())
        begin_str = yes_date_str + ' 17:20:00'
        end_str = today_str + ' 00:00:00'
    elif mid_time_t1.time() <= now_date_time < mid_time_t2.time():
        begin_str = today_str + ' 00:00:00'
        end_str = today_str + ' 12:40:00'
    else:
        begin_str = today_str + ' 12:40:00'
        end_str = today_str + ' 17:20:00'
    begin_time = datetime.strptime(begin_str, '%Y-%m-%d %H:%M:%S')
    end_time = datetime.strptime(end_str, '%Y-%m-%d %H:%M:%S')
    # ?????????????????????????????????
    summary_datetime = now_date-timedelta(minutes=30)
    summary_date = summary_datetime.date()
    return begin_time, end_time, summary_date

# ??????12???40 ??? 17???20 ??? ?????? ??????????????????
@action(MessageAction.SUMMARY_DAILY)
def summary_daily_data(payload, msg_id):
    begin_time, end_time, summary_date = get_summary_daily_time()
    call_actions = (CallActionsR.select(CallActionsR.id,
                                        CallActionsR.bomber_id,
                                        CallActionsR.application_id,
                                        CallActionsR.promised_date,
                                        CallActionsR.cycle,
                                        CallActionsR.name,
                                        CallActionsR.number)
                                .where(CallActionsR.created_at >= begin_time,
                                       CallActionsR.created_at < end_time,
                                       CallActionsR.type << (0,1)))
    summary_dailys = {}
    for call in call_actions:
        if call.bomber_id not in summary_dailys:
            summary_dailys[call.bomber_id] = {'ptp_cnt': 0,
                                             'call_cnt': 0,
                                             'cycle': call.cycle,
                                             'repayment': 0,
                                             'bomber_id': call.bomber_id,
                                             'summary_date':str(summary_date)}

        # C2,C3??????p????????????????????????number???name?????????
        if call.name and call.number:
            summary_dailys[call.bomber_id]['call_cnt'] += 1

        if call.promised_date:
            summary_dailys[call.bomber_id]['ptp_cnt'] += 1

    # ??????????????????
    C1_sql = """
            SELECT a.current_bomber_id,
                   sum(principal_part+late_fee_part) as pay_amount,a.cycle
            from 
                (select a.cycle,a.current_bomber_id,b.username,a.principal_part,
                        a.late_fee_part,a.application_id,a.repay_at
                FROM bomber.repayment_log a ,bomber.bomber b
                WHERE a.repay_at >= '%s' AND a.repay_at <'%s'
                AND a.current_bomber_id !=''
                AND a.current_bomber_id = b.id
                and b.role_id in (1,2,4,5)
                and principal_part+late_fee_part>0
                group by 6,7) a
            GROUP BY a.cycle,a.current_bomber_id
        """ % (begin_time, end_time)
    C1_repayment = run_all_sql(C1_sql)
    other_sql = """
            select current_bomber_id,sum(pay_amount) as pay_amount,cycle
            from (
            select application_id,current_bomber_id,pay_amount,repay_at,cycle
            from (
            select br.application_id,br.current_bomber_id,
                   br.principal_part+br.late_fee_part as pay_amount,br.repay_at,
                   br.cycle
                         from bomber.repayment_log br
                         left join bomber.bomber bb on br.current_bomber_id=bb.id
            where exists (select 1 from bomber.bombing_history bb 
                          where br.current_bomber_id=bb.bomber_id 
                            and br.application_id=bb.application_id 
                            and bb.created_at<br.repay_at 
                            and (bb.promised_date is not null 
                                 or bb.promised_amount is not null))
            and br.repay_at >= '%s'
            and br.repay_at < '%s'
            and bb.role_id in (3,6,7,8,9) 
            and br.principal_part+br.late_fee_part > 0
            group by 1,4
            ) a
            group by 1,4) b
            group by 1
        """ % (begin_time, end_time)
    other_repayment = run_all_sql(other_sql)
    all_repayment = C1_repayment + other_repayment
    for res in all_repayment:
        bomber_id,pay_amount,cycle = res
        if bomber_id in summary_dailys:
            summary_dailys[bomber_id]['repayment'] += pay_amount
        else:
            summary_dailys[bomber_id] = {'ptp_cnt': 0,
                                         'call_cnt': 0,
                                         'cycle': cycle,
                                         'repayment': pay_amount,
                                         'bomber_id': bomber_id,
                                         'summary_date': str(summary_date)
                                         }
    insert_values = list(summary_dailys.values())
    if insert_values:
        SummaryDaily.insert_many(insert_values).execute()

# ?????????cycle?????????????????????
def get_cycle_all_no_paid_app(cycle, type=None):
    apps = (Application
            .select(Application.id,
                    Application.latest_bomber_id,
                    Application.ptp_bomber,
                    Application.promised_date,
                    Application.cycle)
            .where(Application.cycle == cycle,
                   Application.status != ApplicationStatus.REPAID.value,
                   Application.type == type)
            .dicts())

    dis_app_ids = [a['id'] for a in apps]
    # ???dispatch_app?????????????????????
    with db.atomic():
        for idx in range(0, len(dis_app_ids), 1000):
            ids = dis_app_ids[idx:idx + 1000]
            q = (DispatchApp.update(status = DisAppStatus.ABNORMAL.value)
                 .where(DispatchApp.application << ids)
                 .execute())
    return apps

# ??????bomber_id??????app
def get_app_logs(apps):
    app_logs = {}
    all_np_apps = []
    all_p_apps = []
    for a in apps:
        latest_bomber = a["latest_bomber"]
        # 2 ??????????????????latest_bomber??????????????????
        latest_bomber = a["cycle"] if not latest_bomber else latest_bomber
        if latest_bomber in app_logs:
            app_logs[latest_bomber]["to_ids"].append(a["id"])
        else:
            app_logs[latest_bomber] = {"bomber_id": latest_bomber,
                                       "to_ids": [a["id"]],
                                       "np_ids": [],
                                       "p_ids": []}
        if (a["promised_date"] and
                a["promised_date"].date() >= datetime.now().date()):
            app_logs[latest_bomber]["p_ids"].append(a["id"])
            all_p_apps.append(a)
        else:
            app_logs[latest_bomber]["np_ids"].append(a["id"])
            all_np_apps.append(a)
    return app_logs, all_np_apps, all_p_apps

# ???????????????????????????
def month_dispatch_app_out_partner(cycle,apps,app_logs,np_apps):
    # ?????????
    apps = list(apps)
    np_apps = list(np_apps)
    random.shuffle(np_apps)
    apps_len = len(apps)
    np_apps_len = len(np_apps)
    end = 0
    all_app_precentage = 0
    # ????????????cycle??????????????????
    partners = (Partner.select()
                .where(Partner.cycle == cycle,
                       Partner.status == PartnerStatus.NORMAL.value))
    for p in partners:
        all_app_precentage += p.app_percentage

    for partner in partners:
        # ??????????????????
        bombers = (Bomber.select()
                   .where(Bomber.partner == partner.id,
                          Bomber.is_del == 0,
                          Bomber.status != BomberStatus.OUTER_LEADER.value))
        bids = {b.id:b for b in bombers}
        if len(bids) == 0:
            logging.info("cycle:%s,partner:%s,no bomber"%(cycle, partner.id))
            continue
        start = end
        if np_apps_len >= int(apps_len * all_app_precentage):
            end = start + int(apps_len * partner.app_percentage)
        else:
            end = (start +
                   int(np_apps_len * partner.app_percentage / all_app_precentage))
        # ???????????????????????????????????????
        partner_app = np_apps[start:end]
        dispatch_apps_to_bomber(cycle, partner_app, bids, app_logs)
    # ???????????????????????????
    np_apps = np_apps[end:]
    return np_apps


# ???????????????
def month_dispatch_app_inner(cycle,np_apps,app_logs,p_apps):
    sys_cycle = {1: 'AB_TEST_C1A',
                 2: 'AB_TEST_C1B',
                 3: 'AB_TEST_C2',
                 4: 'AB_TEST_C3'}
    # ?????????????????????
    sys_config = SystemConfig.get(SystemConfig.key == sys_cycle[cycle])
    sys_values = json.loads(sys_config.value)
    bombers = (Bomber.select().where(Bomber.id << sys_values,
                                     Bomber.is_del == 0))
    if cycle in (Cycle.C1B.value, Cycle.C2.value, Cycle.C3.value):
        bombers = bombers.where(Bomber.instalment == 0)
    bids = {b.id:b for b in bombers}
    # c1b?????????p????????????????????????
    if cycle == Cycle.C1A.value:
        np_ids = [a["id"] for a in np_apps]
        # ???????????????p??????
        np = (Application
              .update(status = ApplicationStatus.PROCESSING.value,
                      ptp_bomber = None,
                      latest_bomber = None)
              .where(Application.id << np_ids)
              .execute())
        bomber_app_logs = app_logs.get(cycle, {})
        # ?????????????????????,???????????????????????????????????????????????????
        out_param = {
            "application_ids": bomber_app_logs.get("to_ids", []),
            "month_dispatch": 1,
            "src_bomber_id": cycle,
        }
        new_out_record(**out_param)
        in_param = {
            "cycle": cycle,
            "application_ids": np_ids,
            "dest_bomber_id": cycle
        }
        new_in_record(**in_param)
        bomber_app_logs["need_num"] = len(np_apps)
        bomber_app_logs["form_ids"] = np_ids
        bomber_app_logs["status"] = 1
    else:
        dispatch_apps_to_bomber(cycle, np_apps, bids, app_logs, False)

    dispatch_apps_to_bomber(cycle, p_apps, bids, app_logs, False)

# ????????????bomber
def dispatch_apps_to_bomber(cycle,apps,bids,app_logs,out_partner=True,
                            type=ApplicationType.CASH_LOAN.value):
    apps = list(apps)
    random.shuffle(apps)
    # ??????????????????????????????
    bids_list = list(bids.keys())
    if len(bids_list) <= 0:
        logging.info("get_dispatch_app_to_bomber no bids")
        return
    average_num = get_average_number(len(apps), len(bids_list))
    bomber_end = 0
    with db.atomic():
        for index, bid in enumerate(bids_list):
            current_bomber = bids.get(bid)
            bomber_app_logs = app_logs.get(bid, {})
            bomber_start = bomber_end
            bomber_end = bomber_start + average_num[index]
            bomber_apps = apps[bomber_start:bomber_end]
            from_p, from_np, from_ids,status = [], [], [], 0
            # ???????????????????????????????????????p??????????????????p???
            for ba in bomber_apps:
                promised_date = ba.get("promised_date")
                from_ids.append(ba["id"])
                if promised_date and promised_date.date() >= date.today():
                    from_p.append(ba["id"])
                else:
                    from_np.append(ba["id"])
            app_status = ApplicationStatus.AB_TEST.value
            # c1A?????????p?????????????????????
            if (cycle == Cycle.C1A.value and not out_partner
                    and type == ApplicationType.CASH_LOAN.value):
                app_status = ApplicationStatus.PROCESSING.value
            if from_p:
                p = (Application
                     .update(ptp_bomber=bid,
                             latest_bomber=bid,
                             status=app_status)
                     .where(Application.id << from_p)
                     .execute())
                p_ids = bomber_app_logs.get("p_ids", []) + from_p
                bomber_app_logs["p_ids"] = p_ids
            if from_np:
                np = (Application
                      .update(latest_bomber=bid,
                              ptp_bomber=None,
                              status=ApplicationStatus.AB_TEST.value)
                      .where(Application.id << from_np)
                      .execute())
                np_ids = bomber_app_logs.get("np_ids", []) + from_np
                bomber_app_logs["np_ids"] = np_ids
            in_param = {"cycle": cycle,
                        "dest_partner_id": current_bomber.partner_id,
                        "application_ids": from_ids,
                        "dest_bomber_id": bid,
                        }
            if type == ApplicationType.CASH_LOAN.value:
                out_param = {"src_bomber_id": bid,
                             "application_ids": bomber_app_logs.get("to_ids",[]),
                             "month_dispatch":1
                             }
                # ??????
                new_out_record(**out_param)
                # ??????
                new_in_record(**in_param)
            else:
                out_and_in_record_instalment(**in_param)
            bomber_app_logs["status"] = 1
            need_num = bomber_app_logs.get("need_num", 0) + average_num[index]
            bomber_app_logs["need_num"] = need_num
            all_form_ids = bomber_app_logs.get("form_ids", []) + from_ids
            bomber_app_logs["form_ids"] = all_form_ids
            # ??????????????????????????????????????????????????????
            if not out_partner:
                continue
            # ?????????????????????????????????dispatch_app???.????????????????????????,?????????????????????
            try:
                (DispatchApp.delete()
                 .where(DispatchApp.application.in_(from_ids))
                 .execute())
                dispatch_ins = [{"application": id,
                                 "partner": current_bomber.partner_id,
                                 "bomber": bid,
                                 "status": DisAppStatus.NORMAL.value,
                                 } for id in from_ids]
                (DispatchApp.insert_many(dispatch_ins).execute())
            except Exception as e:
                logging.info(
                    "month_disapp_error error:%s,bid:%s,from_ids:%s" %
                    (str(e), bid, from_ids))


# ??????????????????????????????,?????????????????????????????????cycle
def calc_instalment_apps_cycle():
    cycle_list = [Cycle.C2.value, Cycle.C3.value]
    for cycle in cycle_list:
        apps = (ApplicationR.select(ApplicationR.id,
                                    ApplicationR.cycle,
                                    ApplicationR.overdue_days.alias("ods"),
                                    ApplicationR.latest_bomber,
                                    OverdueBillR.status,
                                    OverdueBillR.overdue_days.alias("oods"))
                .join(OverdueBillR, JOIN_LEFT_OUTER,
                      on=ApplicationR.id == OverdueBillR.collection_id)
                .where(ApplicationR.cycle == cycle,
                       ApplicationR.type ==
                       ApplicationType.CASH_LOAN_STAGING.value,
                       ApplicationR.status != ApplicationStatus.REPAID.value)
                .dicts())
        # ????????????????????????overdue_days
        lower_apps = {}
        for app in apps:
            if app["status"] == ApplicationStatus.REPAID.value:
                continue
            aid = app["id"]
            if aid in lower_apps:
                lower_apps[aid]["ods"] = max(app["oods"], app["ods"])
            else:
                lower_apps[aid] = {
                    "id": aid,
                    "cycle": cycle,
                    "ods": app["oods"],
                }
        # ??????apps????????????????????????cycle????????????
        for aid,app in lower_apps.items():
            new_cycle = get_cycle_by_overdue_days(app["ods"])
            if new_cycle != cycle:
                update_param = {"cycle":new_cycle,
                                "overdue_days":app["ods"]}
                entry_time = calc_entry_time(app["ods"])
                update_param.update(entry_time)
                # ???????????????
                (Application.update(**update_param)
                 .where(Application.id == aid)
                 .execute())


# ???cycle????????????????????????????????????????????????
def calc_entry_time(overdue_days):
    app_entry_time = {}
    overdue_entry = {
        "dpd1_entry": [1, 3],
        "C1A_entry": [4, 10],
        "C1B_entry": [11, 30],
        "C2_entry": [31, 60],
        "C3_entry": [61, 90]
    }
    for key,value in overdue_entry.items():
        if value[0] <= overdue_days <= value[1]:
            app_entry_time[key] = datetime.now()
        else:
            app_entry_time[key] = None
    return app_entry_time

# ????????????
def instalment_month_dispatch_app():
    sys_cycle = {1: 'AB_TEST_C1A',
                 2: 'AB_TEST_C1B',
                 3: 'AB_TEST_C2',
                 4: 'AB_TEST_C3'}
    # ???cycle
    calc_instalment_apps_cycle()
    instalment_cycle_list = Cycle.values()[:4]
    for cycle in instalment_cycle_list:
        apps = get_cycle_all_no_paid_app(cycle,
                                         ApplicationType.CASH_LOAN_STAGING.value)
        if not apps:
            logging.info("instalment_month_dispatch no get apps,cycle:%s"%cycle)
            continue
        app_logs, all_np_apps, all_p_apps = get_app_logs(apps)
        # ????????????????????????
        if cycle == Cycle.C1A.value:
            sys_config = SystemConfig.get(SystemConfig.key == sys_cycle[cycle])
            sys_values = json.loads(sys_config.value)
            bombers = (Bomber.select().where(Bomber.id << sys_values,
                                             Bomber.is_del == 0))
        else:
            bombers = (Bomber.select().where(Bomber.is_del == 0,
                                             Bomber.instalment == cycle))
        bids = {b.id:b for b in bombers}
        if not bids:
            logging.info("instalment_month_dispatch no bomber,cycle:%s"%cycle)
            continue
        dispatch_apps_to_bomber(cycle = cycle,
                                apps = all_p_apps,
                                bids = bids,
                                app_logs = app_logs,
                                out_partner = False,
                                type = ApplicationType.CASH_LOAN_STAGING.value)
        if cycle in (Cycle.C1B.value, Cycle.C2.value, Cycle.C3.value):
            dispatch_apps_to_bomber(cycle=cycle,
                                    apps=all_np_apps,
                                    bids=bids,
                                    app_logs=app_logs,
                                    out_partner=False,
                                    type=ApplicationType.CASH_LOAN_STAGING.value)
        else:
            # ??????p????????????????????????
            np_ids = [a["id"] for a in all_np_apps]
            np = (Application.update(status=ApplicationStatus.UNCLAIMED.value,
                                     ptp_bomber=None,
                                     latest_bomber=None)
                  .where(Application.id << np_ids,
                         ApplicationStatus != ApplicationStatus.REPAID.value)
                  .execute())
            in_param = {
                "cycle": cycle,
                "application_ids": np_ids,
                "dest_bomber_id": cycle
            }
            out_and_in_record_instalment(**in_param)

        # ????????????cycle????????????????????????????????????
        try:
            dispatch_apps_logs = []
            for bid,app in app_logs.items():
                alg = {
                    "bomber_id": bid,
                    "need_num": -len(app.get("to_ids", [])),
                    "form_ids": json.dumps(app.get("form_ids", [])),
                    "to_ids": json.dumps(app.get("to_ids", [])),
                    "np_ids": json.dumps(app.get("np_ids", [])),
                    "p_ids": json.dumps(app.get("p_ids", [])),
                    "status": 1
                }
                if bid in bids:
                    alg["need_num"] = app.get("need_num", 0)
                dispatch_apps_logs.append(alg)
            if dispatch_apps_logs:
                DispatchAppLogs.insert_many(dispatch_apps_logs).execute()
        except Exception as e:
            logging.info(
                "instalment_dispatch_app_month log error.cycle:%s,error:%s" % (
                    cycle, str(e)))


# ??????????????????????????????????????????
@action(MessageAction.MONTH_DISPATCH_APP)
def month_dispatch_app(payload, msg_id):
    # ??????????????????????????????1???
    if datetime.today().day != 1:
        logging.info("????????????1???,?????????????????????")
        return
    cycle_list = [Cycle.C1A.value,
                  Cycle.C1B.value,
                  Cycle.C2.value,
                  Cycle.C3.value]
    with db.atomic():
        for cycle in cycle_list:
            apps = get_cycle_all_no_paid_app(cycle,
                                             ApplicationType.CASH_LOAN.value)
            if not apps:
                logging.info("month_dispatch_app not get apps.cycle:%s"%cycle)
                continue
            app_logs, all_np_apps, all_p_apps = get_app_logs(apps)
            np_apps = month_dispatch_app_out_partner(cycle=cycle,
                                                     apps=apps,
                                                     app_logs=app_logs,
                                                     np_apps = all_np_apps)
            if not np_apps and not all_p_apps:
                logging.info("month_dispatch_app not get inner apps.cycle:%s",
                             cycle)
                continue
            month_dispatch_app_inner(cycle,np_apps,app_logs,all_p_apps)
            # ???????????????????????????
            try:
                dispatch_apps_logs = []
                for bid,app in app_logs.items():
                    alg = {
                        "bomber_id": bid,
                        "need_num": app.get("need_num",0),
                        "form_ids": json.dumps(app.get("form_ids", [])),
                        "to_ids": json.dumps(app.get("to_ids", [])),
                        "np_ids": json.dumps(app.get("np_ids", [])),
                        "p_ids": json.dumps(app.get("p_ids", [])),
                        "status": 1
                    }
                    dispatch_apps_logs.append(alg)
                for idx in range(0, len(dispatch_apps_logs), 10):
                    DispatchAppLogs.insert_many(
                        dispatch_apps_logs[idx:idx + 10]).execute()
            except Exception as e:
                logging.error(
                    "insert dispatch_log error:%s,cycle:%s"%(str(e),cycle))
        try:
            instalment_month_dispatch_app()
        except Exception as e:
            logging.info("instalment_month_dispatch_error:%s"%str(e))


# ?????????????????????????????????
@action(MessageAction.SUMMARY_BOMBER_OVERDUE)
def summary_bomber_overdue_everyday(payload, msg_id):
    cycle_list = Cycle.values()
    which_day = date.today()
    # ????????????cycle?????????????????????
    for cycle in cycle_list:
        apps = (ApplicationR.select(ApplicationR.id,
                                    ApplicationR.cycle,
                                    ApplicationR.ptp_bomber,
                                    ApplicationR.overdue_days,
                                    ApplicationR.promised_date,
                                    ApplicationR.follow_up_date,
                                    ApplicationR.external_id,
                                    OverdueBillR.status,
                                    OverdueBillR.periods,
                                    OverdueBillR.sub_bill_id)
                .join(OverdueBillR, JOIN_LEFT_OUTER,
                      on = ApplicationR.id == OverdueBillR.collection_id)
                .where(ApplicationR.status != ApplicationStatus.REPAID.value,
                       ApplicationR.no_active == 0,
                       ApplicationR.cycle == cycle)
                .dicts())

        bomber_overdue_list = []
        for app in apps:
            status = app.get("status")
            if status == ApplicationStatus.REPAID.value:
                continue
            ptp_bomber = app.get("ptp_bomber")
            promised_date = app.get("promised_date")
            follow_up_date = app.get("follow_up_date")
            if not promised_date or promised_date.date() < date.today():
                ptp_bomber = promised_date = None
            if not follow_up_date or follow_up_date.date() < date.today():
                follow_up_date = None
            overdue_dict = {
                "collection_id": app.get("id"),
                "external_id": app.get("external_id"),
                "sub_bill_id": app.get("sub_bill_id"),
                "periods": app.get("periods"),
                "cycle": app.get("cycle") if app.get("cycle") else cycle,
                "ptp_bomber": ptp_bomber,
                "promised_date": promised_date,
                "follow_up_date": follow_up_date,
                "which_day": which_day,
                "overdue_days": app.get("overdue_days")
            }
            bomber_overdue_list.append(overdue_dict)
        try:
            if bomber_overdue_list:
                with db.atomic():
                    for index in range(0, len(bomber_overdue_list), 1000):
                        insert_list = bomber_overdue_list[index: index+1000]
                        BomberOverdue.insert_many(insert_list).execute()
        except Exception as e:
            logging.info(
                "summary_bomber_overdue_error,cycle:%s,which_day:%s,error:%s"%(
                    cycle,str(which_day),str(e)))

# ????????????????????????p?????????????????????
@action(MessageAction.BOMBER_PTP_REAL_TIME_SUMMARY)
def bomber_ptp_real_time_summary(payload, msg_id):
    ptp_switch_number = 200
    sys_ptp_switch = (SystemConfig.select()
                     .where(SystemConfig.key == 'PTP_SWITCH_NUMBER')
                     .first())
    if sys_ptp_switch and sys_ptp_switch.value.isdigit():
        ptp_switch_number = int(sys_ptp_switch.value)
    today = datetime.today().date()
    ptp_apps = (ApplicationR.select(fn.COUNT(ApplicationR.id).alias('ptp_cnt'),
                                    ApplicationR.latest_bomber)
                .where(ApplicationR.status != ApplicationStatus.REPAID.value,
                       ApplicationR.cycle < Cycle.C2.value,
                       ApplicationR.promised_date >= today,
                       ApplicationR.latest_bomber.is_null(False))
                .group_by(ApplicationR.latest_bomber))

    bomber_ptps = (BomberPtp.select(BomberPtp.bomber_id))
    bomber_ptp_bids = [b.bomber_id for b in bomber_ptps]
    insert_result = []
    for app in ptp_apps:
        ptp_switch = BomberCallSwitch.ON.value
        if app.ptp_cnt >= ptp_switch_number:
            ptp_switch = BomberCallSwitch.OFF.value
        params = {"bomber_id": app.latest_bomber_id,
                  "ptp_cnt": app.ptp_cnt,
                  "ptp_switch": ptp_switch,
                  "auto_ext": app.latest_bomber.auto_ext}
        if app.latest_bomber_id in bomber_ptp_bids:
            try:
                q = (BomberPtp.update(**params)
                     .where(BomberPtp.bomber_id==app.latest_bomber_id)
                     .execute())
            except Exception as e:
                logging.error("ptp_reil_time_summary_error:%s,data,bid:%s" % (
                    str(e),params,app.latest_bomber_id))
        else:
            insert_result.append(params)
    if insert_result:
        BomberPtp.insert_many(insert_result).execute()

# ?????????10:00???14:00,16:30?????????????????????,????????????????????????????????????,?????????????????????
@action(MessageAction.BOMBER_TODAY_PTP_FOLLOW_SWITCH_OFF)
def today_ptp_auto_call_switch(payload, msg_id):
    today = datetime.today().date()
    next_day = today + timedelta(days=1)
    # ???????????????p????????????????????????
    apps = (ApplicationR.select(ApplicationR.latest_bomber)
            .where(ApplicationR.promised_date < next_day,
                   ApplicationR.promised_date >= today,
                   ApplicationR.promised_date.is_null(False),
                   ApplicationR.status != ApplicationStatus.REPAID.value,
                   ApplicationR.cycle < Cycle.C2.value,
                   ApplicationR.latest_bomber.is_null(False))
            .group_by(ApplicationR.latest_bomber))
    bids = [a.latest_bomber_id for a in apps]
    if not bids:
        return
    q = (BomberPtp.update(today_switch=BomberCallSwitch.OFF.value)
         .where(BomberPtp.auto_ext.is_null(False),
                BomberPtp.bomber_id << bids)
         .execute())

# ????????????8?????????????????????????????????????????????
@action(MessageAction.BOMBER_TODAY_PTP_FOLLOW_SWITCH_ON)
def update_today_switch_every_day(payload, msg_id):
    q = (BomberPtp.update(today_switch=BomberCallSwitch.ON.value)
         .where(BomberPtp.auto_ext.is_null(False))
         .execute())

# ????????????????????????bomber
@action(MessageAction.USER_UPDATE_PHONE)
def user_change_phone(payload, msg_id):
    user_id = payload.get("user_id")
    new_mobile_no = payload.get("new_mobile_no")
    if not all([user_id, new_mobile_no]):
        logging.info("??????????????????,?????????????????????id?????????????????????")
        return
    source = 'applicant updated number'
    contacts = (Contact.select()
               .where(Contact.user_id == int(user_id)))
    if not contacts.exists():
        logging.info("?????????contact???????????????")
        return
    new_contact = contacts.where(Contact.number == new_mobile_no,
                                 Contact.source == source)
    if new_contact.exists():
        logging.info("????????????????????????")
        return
    contact = contacts.order_by(-Contact.created_at).first()
    Contact.create(user_id=contact.user_id,
                   name=contact.name,
                   number = new_mobile_no,
                   source = source,
                   relationship = Relationship.APPLICANT.value,
                   real_relationship = Relationship.APPLICANT.value)

