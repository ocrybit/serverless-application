# -*- coding: utf-8 -*-
import os
import settings
import time
import json
import requests
from boto3.dynamodb.conditions import Key
from db_util import DBUtil
from user_util import UserUtil
from lambda_base import LambdaBase
from jsonschema import validate, ValidationError
from time_util import TimeUtil
from record_not_found_error import RecordNotFoundError
from exceptions import SendTransactionError
from aws_requests_auth.aws_auth import AWSRequestsAuth
from decimal_encoder import DecimalEncoder
from time import sleep
from decimal import Decimal


class MeArticlesPurchaseCreate(LambdaBase):
    def get_schema(self):
        return {
            'type': 'object',
            'properties': {
                'article_id': settings.parameters['article_id'],
                'price': settings.parameters['price']
            },
            'required': ['article_id', 'price']
        }

    def validate_params(self):
        UserUtil.verified_phone_and_email(self.event)

        # check price type is integer or decimal
        try:
            self.params['price'] = int(self.params['price'])
        except ValueError:
            raise ValidationError('Price must be integer')

        # check price value is not decimal
        price = self.params['price'] / 10 ** 18
        if price.is_integer() is False:
            raise ValidationError('Decimal value is not allowed')

        # single
        validate(self.params, self.get_schema())
        # relation
        DBUtil.validate_article_existence(
            self.dynamodb,
            self.params['article_id'],
            status='public'
        )
        # validate latest price
        DBUtil.validate_latest_price(
            self.dynamodb,
            self.params['article_id'],
            self.params['price']
        )

    def exec_main_proc(self):
        # get article info
        article_info_table = self.dynamodb.Table(os.environ['ARTICLE_INFO_TABLE_NAME'])
        article_info = article_info_table.get_item(Key={'article_id': self.params['article_id']}).get('Item')
        # does not purchase same user's article
        if article_info['user_id'] == self.event['requestContext']['authorizer']['claims']['cognito:username']:
            raise ValidationError('Can not purchase own article')

        # purchase article
        paid_articles_table = self.dynamodb.Table(os.environ['PAID_ARTICLES_TABLE_NAME'])
        article_user_eth_address = self.__get_user_private_eth_address(article_info['user_id'])
        user_eth_address = self.event['requestContext']['authorizer']['claims']['custom:private_eth_address']
        price = self.params['price']

        auth = AWSRequestsAuth(aws_access_key=os.environ['PRIVATE_CHAIN_AWS_ACCESS_KEY'],
                               aws_secret_access_key=os.environ['PRIVATE_CHAIN_AWS_SECRET_ACCESS_KEY'],
                               aws_host=os.environ['PRIVATE_CHAIN_EXECUTE_API_HOST'],
                               aws_region='ap-northeast-1',
                               aws_service='execute-api')
        headers = {'content-type': 'application/json'}

        # 購入のトランザクション処理
        purchase_transaction = self.__create_purchase_transaction(auth, headers, user_eth_address,
                                                                  article_user_eth_address, price)
        # 購入テーブルを作成
        self.__purchase_article(paid_articles_table, article_info, purchase_transaction)
        # バーンのトランザクション処理
        burn_transaction = self.__burn_transaction(price, user_eth_address, auth, headers)
        # バーンのトランザクションを購入テーブルに格納
        self.__add_burn_transaction_to_paid_article(burn_transaction, paid_articles_table, article_info)
        # プライベートチェーンへのポーリングを行いトランザクションの承認状態を取得
        transaction_status = self.__polling_to_private_chain(purchase_transaction, auth, headers)
        # トランザクションの承認状態をpaid_artilcleに格納
        self.__add_transaction_status_to_paid_article(article_info, paid_articles_table, transaction_status)

        return {
            'statusCode': 200,
            'body': json.dumps({
                'status': transaction_status
            })
        }

    @staticmethod
    def __create_purchase_transaction(auth, headers, user_eth_address, article_user_eth_address, price):
        purchase_price = format(int(Decimal(price) * Decimal(9) / Decimal(10)), '064x')
        purchase_payload = json.dumps(
            {
                'from_user_eth_address': user_eth_address,
                'to_user_eth_address': article_user_eth_address[2:],
                'tip_value': purchase_price
            }
        )
        # purchase article transaction
        response = requests.post('https://' + os.environ['PRIVATE_CHAIN_EXECUTE_API_HOST'] +
                                 '/production/wallet/tip', auth=auth, headers=headers, data=purchase_payload)

        # exists error
        if json.loads(response.text).get('error'):
            raise SendTransactionError(json.loads(response.text).get('error'))

        return json.loads(response.text).get('result').replace('"', '')

    def __purchase_article(self, paid_articles_table, article_info, purchase_transaction):
        article_history_table = self.dynamodb.Table(os.environ['ARTICLE_HISTORY_TABLE_NAME'])
        article_histories = article_history_table.query(
            KeyConditionExpression=Key('article_id').eq(self.params['article_id']),
            ScanIndexForward=False
        )['Items']
        history_created_at = ''
        # 降順でarticle_infoの価格と同じ一番新しい記事historyデータを取得する
        for Item in json.loads(json.dumps(article_histories, cls=DecimalEncoder)):
            if Item.get('price') is not None and Item.get('price') == article_info['price']:
                history_created_at = Item.get('created_at')
                break

        # burnのtransactionが失敗した場合に購入transactionを追跡するためにburn_transaction以外を保存
        paid_article = {
            'article_id': self.params['article_id'],
            'user_id': self.event['requestContext']['authorizer']['claims']['cognito:username'],
            'article_user_id': article_info['user_id'],
            'article_title': article_info['title'],
            'purchase_transaction': purchase_transaction,
            'sort_key': TimeUtil.generate_sort_key(),
            'price': self.params['price'],
            'history_created_at': history_created_at,
            'created_at': int(time.time())
        }
        # 購入時のtransactionの保存
        paid_articles_table.put_item(
            Item=paid_article,
            ConditionExpression='attribute_not_exists(user_id)'
        )

        return purchase_transaction

    @staticmethod
    def __burn_transaction(price, user_eth_address, auth, headers):
        burn_token = format(int(Decimal(price) / Decimal(10)), '064x')

        burn_payload = json.dumps(
            {
                'from_user_eth_address': user_eth_address,
                'to_user_eth_address': settings.ETH_ZERO_ADDRESS,
                'tip_value': burn_token
            }
        )

        # burn transaction
        response = requests.post('https://' + os.environ['PRIVATE_CHAIN_EXECUTE_API_HOST'] +
                                 '/production/wallet/tip', auth=auth, headers=headers, data=burn_payload)

        # exists error
        if json.loads(response.text).get('error'):
            raise SendTransactionError(json.loads(response.text).get('error'))

        return json.loads(response.text).get('result').replace('"', '')

    def __add_burn_transaction_to_paid_article(self, burn_transaction, paid_articles_table, article_info):
        paid_article = {
            ':burn_transaction': burn_transaction
        }
        paid_articles_table.update_item(
            Key={
                'article_id': article_info['article_id'],
                'user_id': self.event['requestContext']['authorizer']['claims']['cognito:username']
            },
            UpdateExpression="set burn_transaction = :burn_transaction",
            ExpressionAttributeValues=paid_article
        )

    def __add_transaction_status_to_paid_article(self, article_info, paid_articles_table, transaction_status):
        paid_articles_table.update_item(
            Key={
                'article_id': article_info['article_id'],
                'user_id': self.event['requestContext']['authorizer']['claims']['cognito:username']
            },
            UpdateExpression="set #attr = :transaction_status",
            ExpressionAttributeNames={'#attr': 'status'},
            ExpressionAttributeValues={':transaction_status': transaction_status}
        )

        return {'status': transaction_status}

    def __get_user_private_eth_address(self, user_id):
        # user_id に紐づく private_eth_address を取得
        user_info = UserUtil.get_cognito_user_info(self.cognito, user_id)
        private_eth_address = [a for a in user_info['UserAttributes'] if a.get('Name') == 'custom:private_eth_address']
        # private_eth_address が存在しないケースは想定していないため、取得出来ない場合は例外とする
        if len(private_eth_address) != 1:
            raise RecordNotFoundError('Record Not Found: private_eth_address')

        return private_eth_address[0]['Value']

    @staticmethod
    def __check_transaction_confirmation(purchase_transaction, auth, headers):
        receipt_payload = json.dumps(
            {
                'transaction_hash': purchase_transaction
            }
        )
        response = requests.post('https://' + os.environ['PRIVATE_CHAIN_EXECUTE_API_HOST'] +
                                 '/production/transaction/receipt', auth=auth, headers=headers, data=receipt_payload)
        return response.text

    def __polling_to_private_chain(self, purchase_transaction, auth, headers):
        # 最大3回トランザクション詳細を問い合わせる
        count = settings.POLLING_INITIAL_COUNT
        while count < settings.POLLING_MAX_COUNT:
            count += 1
            # 1秒待機
            sleep(1)
            # check whether transaction is completed
            transaction_info = self.__check_transaction_confirmation(purchase_transaction, auth, headers)
            result = json.loads(transaction_info).get('result')
            # exists error
            if json.loads(transaction_info).get('error'):
                return 'fail'
            # receiptがnullの場合countをインクリメントしてループ処理
            if result is None or result['logs'] == 0:
                continue
            # transactionが承認済みであればstatusをdoneにする
            if result['logs'][0].get('type') == 'mined':
                return 'done'
        return 'doing'
